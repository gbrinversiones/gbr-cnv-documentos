"""Llamadas al modelo en paralelo (financieros, resumenes, clasificar, avisos-pago, condiciones, eventos) con las mismas garantias que el bucle secuencial.

Reparto de tareas:
  - un pool de hilos acotado hace SOLO el trabajo caro y sin efectos (leer el texto y llamar al modelo; las reglas de verificacion de citas no cambian);
  - el hilo principal CONFIRMA los resultados (anota el estado, guarda en el almacen, suma los totales y escribe el log) de a uno, en el orden en que se
    enviaron los documentos. Por eso hay una sola escritura al almacen a la vez (ESCRITURA lo hace explicito), se guarda tras cada documento como antes y la
    salida es determinista (mismo orden que sin paralelismo);
  - los topes (--max, --tiempo-max) dejan de ENVIAR trabajo nuevo y esperan a los que estan en vuelo: no se pierde ningun resultado ya pagado;
  - un hilo que falla con un error inesperado no pierde a los demas: ese documento queda pendiente para la proxima corrida;
  - ante 429 / 5xx / «overloaded» se reintenta con espera creciente respetando el header `retry-after`; si los limites de tasa se repiten, la concurrencia
    baja sola (y vuelve a subir de a uno cuando la API se normaliza).
Presupuesto de tokens por minuto (CNV_TPM): la clave de la API se COMPARTE con el chatbot de produccion, asi que el pipeline no puede agotar el limite de la
cuenta. Antes de cada pedido se reservan los tokens de entrada estimados (caracteres / 3) en una ventana movil de 60 s y, si no entran, se espera (proactivo; el
limitador de 429 de arriba es solo la red de seguridad, reactiva).
Concurrencia: CNV_CONCURRENCIA (defecto 2, minimo 1, maximo 6). Con 1 todo corre en el hilo principal, sin pool, exactamente como el bucle secuencial anterior.
"""
import os
import random
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

DEFECTO, MAXIMO = 2, 6              # 2 por defecto mientras la clave se comparta con el chatbot de produccion
TPM_DEFECTO = 150_000              # tokens de ENTRADA por minuto: ~30 % del limite de 500.000 de la cuenta; el resto queda libre para produccion
VENTANA_TPM = 60.0
CARACTERES_POR_TOKEN = 3           # estimacion conservadora (el espanol da ~3,5-4): se reserva de mas, nunca de menos
INTENTOS_API = 5                 # llamadas a la API por pedido (1 + 4 reintentos)
ESPERA_MAXIMA = 120.0            # segundos: tope de lo que se espera por un retry-after
ESCRITURA = threading.Lock()     # una sola escritura al almacen a la vez
HTTP_REINTENTABLES = {408, 409, 429, 500, 502, 503, 504, 529}
NOMBRES_REINTENTABLES = {"RateLimitError", "InternalServerError", "OverloadedError", "APIConnectionError", "APITimeoutError"}
NOMBRES_LIMITE = {"RateLimitError", "OverloadedError"}


def hilos() -> int:
    """CNV_CONCURRENCIA acotado a [1, MAXIMO]; si no es un entero, el valor por defecto."""
    try:
        n = int(os.environ.get("CNV_CONCURRENCIA", DEFECTO))
    except ValueError:
        n = DEFECTO
    return max(1, min(MAXIMO, n))


def tpm() -> int:
    """CNV_TPM: tokens de entrada por minuto que el pipeline puede gastar. Un valor no numerico o menor o igual a cero vale el defecto (no se puede apagar)."""
    try:
        n = int(os.environ.get("CNV_TPM", TPM_DEFECTO))
    except ValueError:
        return TPM_DEFECTO
    return n if n > 0 else TPM_DEFECTO


def estimar_tokens(kw: dict) -> int:
    """Tokens de entrada de un pedido a `messages.create`: caracteres del sistema y de los mensajes / CARACTERES_POR_TOKEN, redondeado hacia arriba."""
    texto = str(kw.get("system") or "")
    for m in kw.get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else None
        texto += c if isinstance(c, str) else str(c or "")
    return max(1, -(-len(texto) // CARACTERES_POR_TOKEN))


class Presupuesto:
    """Ventana movil de `limite` tokens por `ventana` segundos, compartida por todos los hilos. `esperar(n)` reserva `n` tokens y duerme hasta que entren.
    Un pedido mas grande que todo el presupuesto se deja pasar cuando la ventana esta vacia (si no, esperaria para siempre)."""

    def __init__(self, limite: int, reloj=time.monotonic, dormir=None, ventana: float = VENTANA_TPM):
        self.limite, self.reloj, self.ventana = limite, reloj, ventana
        self.dormir = dormir or time.sleep
        self.usos = deque()                     # (instante, tokens)
        self.esperado, self.esperas, self._ultimo_aviso = 0.0, 0, None
        self._cerrojo = threading.Lock()

    def _purgar(self, ahora: float) -> int:
        while self.usos and ahora - self.usos[0][0] >= self.ventana:
            self.usos.popleft()
        return sum(t for _, t in self.usos)

    def usado(self) -> int:
        with self._cerrojo:
            return self._purgar(self.reloj())

    def esperar(self, tokens: int) -> float:
        """Reserva `tokens`; devuelve cuantos segundos se esperaron."""
        total_espera = 0.0
        while True:
            with self._cerrojo:
                ahora = self.reloj()
                usado = self._purgar(ahora)
                if usado + tokens <= self.limite or not self.usos:
                    self.usos.append((ahora, tokens))
                    return total_espera
                espera = max(0.05, self.usos[0][0] + self.ventana - ahora)
                self.esperas += 1
                self.esperado += espera
                if self._ultimo_aviso is None or ahora - self._ultimo_aviso >= 30:
                    self._ultimo_aviso = ahora
                    print(f"  presupuesto de tokens ({self.limite}/min): esperando {espera:.0f} s para no ocupar la cuenta compartida con produccion", flush=True)
            self.dormir(espera)
            total_espera += espera


_presupuesto: Presupuesto | None = None
_cerrojo_presupuesto = threading.Lock()


def presupuesto() -> Presupuesto:
    """El presupuesto del proceso, creado la primera vez con CNV_TPM (todos los hilos comparten el mismo)."""
    global _presupuesto
    with _cerrojo_presupuesto:
        if _presupuesto is None:
            _presupuesto = Presupuesto(tpm())
        return _presupuesto


# ---------------------------------------------------------------------------
# Reintentos ante limites de tasa y errores del servidor
# ---------------------------------------------------------------------------

def _estado_http(error) -> int | None:
    s = getattr(error, "status_code", None)
    return s if isinstance(s, int) else None


def es_reintentable(error: Exception) -> bool:
    return (_estado_http(error) in HTTP_REINTENTABLES or type(error).__name__ in NOMBRES_REINTENTABLES or "overloaded" in str(error).lower())


def es_limite_de_tasa(error: Exception) -> bool:
    """429 / 529 / overloaded: la API pide ir mas despacio (a diferencia de un 500 o un corte de red)."""
    return _estado_http(error) in (429, 529) or type(error).__name__ in NOMBRES_LIMITE or "overloaded" in str(error).lower()


MARCAS_CUENTA = ("credit balance", "authentication", "permission", "invalid x-api-key", "invalid api key", "billing")
NOMBRES_CUENTA = {"AuthenticationError", "PermissionDeniedError"}
AVISO_CUENTA = "API sin credito/clave invalida: paso detenido, 0 documentos marcados"


class ErrorDeCuenta(Exception):
    """Error de la CUENTA o de la API (saldo, clave, permisos, limite sostenido), no del documento: no cuenta intento y aborta el paso."""


def es_error_de_cuenta(error: Exception) -> bool:
    """HTTP 400 con «credit balance is too low», 401, 402, 403 (clave invalida, sin saldo, sin permiso) o un limite de tasa que sobrevivio a todos los
    reintentos (ErrorDeCuenta). Un 400 por otro motivo, un JSON invalido o una cita no verificada son errores del documento: NO entran aca."""
    if isinstance(error, ErrorDeCuenta):
        return True
    estado = _estado_http(error)
    if estado in (401, 402, 403) or type(error).__name__ in NOMBRES_CUENTA:
        return True
    return estado == 400 and any(m in str(error).lower() for m in MARCAS_CUENTA)


def espera_para(error: Exception, intento: int, azar=random.random) -> float:
    """Segundos a esperar antes del reintento `intento` (0 = el primero): el header `retry-after` si la API lo mando (tope ESPERA_MAXIMA), si no 2^intento + azar."""
    cabeceras = getattr(getattr(error, "response", None), "headers", None)
    try:
        valor = cabeceras.get("retry-after") if cabeceras is not None else None
        if valor is not None:
            return max(0.0, min(ESPERA_MAXIMA, float(valor)))
    except (TypeError, ValueError, AttributeError):
        pass
    return min(60.0, 2.0 ** (intento + 1) + azar())


class Limitador:
    """Cuantos pedidos en vuelo se permiten. Baja a la mitad (minimo 1) cuando hay `umbral` limites de tasa dentro de `ventana` segundos; sube de a uno
    despues de `exitos_para_subir` pedidos exitosos seguidos, hasta el maximo configurado."""

    def __init__(self, maximo: int, reloj=time.monotonic, ventana: float = 60.0, umbral: int = 3, exitos_para_subir: int = 20):
        self.maximo, self.actual = maximo, maximo
        self.reloj, self.ventana, self.umbral, self.exitos_para_subir = reloj, ventana, umbral, exitos_para_subir
        self.eventos, self.exitos, self.bajadas = deque(), 0, 0
        self._cerrojo = threading.Lock()

    def limite(self) -> None:
        with self._cerrojo:
            ahora = self.reloj()
            self.eventos.append(ahora)
            while self.eventos and ahora - self.eventos[0] > self.ventana:
                self.eventos.popleft()
            self.exitos = 0
            if len(self.eventos) >= self.umbral and self.actual > 1:
                self.actual = max(1, self.actual // 2)
                self.bajadas += 1
                self.eventos.clear()
                print(f"  limite de tasa sostenido: la concurrencia baja a {self.actual}", flush=True)

    def exito(self) -> None:
        with self._cerrojo:
            self.exitos += 1
            if self.exitos >= self.exitos_para_subir and self.actual < self.maximo:
                self.actual += 1
                self.exitos = 0

    def permitidos(self) -> int:
        return self.actual


_limitador: Limitador | None = None          # lo fija ejecutar_lote durante una corrida; fuera de una corrida no hay limitador


def con_reintentos(fn, intentos: int = INTENTOS_API, dormir=None, limitador: Limitador | None = None, azar=random.random):
    """Llama a `fn()`; ante un error reintentable espera (retry-after o espera creciente) y vuelve a llamar, hasta `intentos` veces. Lo demas se propaga ya."""
    dormir = dormir or time.sleep
    limitador = limitador or _limitador
    for n in range(intentos):
        try:
            r = fn()
        except Exception as error:
            if not es_reintentable(error) or n == intentos - 1:
                raise
            if limitador is not None and es_limite_de_tasa(error):
                limitador.limite()
            dormir(espera_para(error, n, azar))
            continue
        if limitador is not None:
            limitador.exito()
        return r


def crear_mensaje(cliente, **kw):
    """`cliente.messages.create(**kw)` con presupuesto de tokens por minuto y reintentos. Es el unico punto por el que pasan las llamadas al modelo.
    Cada intento (tambien los reintentos) reserva sus tokens estimados antes de salir."""
    tokens = estimar_tokens(kw)

    def llamar():
        presupuesto().esperar(tokens)
        return cliente.messages.create(**kw)

    try:
        return con_reintentos(llamar)
    except Exception as error:
        if es_limite_de_tasa(error):                 # 429 / 529 que aguanto todos los reintentos: sostenido, es de la cuenta y no del documento
            raise ErrorDeCuenta(f"limite de tasa sostenido tras {INTENTOS_API} intentos: {type(error).__name__}") from error
        raise


# ---------------------------------------------------------------------------
# Ejecutor
# ---------------------------------------------------------------------------

class Res:
    """Lo que devuelve el trabajo de un documento. `llamo`: si cuenta contra el tope --max (llamo al modelo o lo intento)."""
    __slots__ = ("datos", "llamo")

    def __init__(self, datos, llamo: bool = True):
        self.datos, self.llamo = datos, llamo


def avisar_cuenta(error: Exception) -> None:
    print(f"  {AVISO_CUENTA} ({type(error).__name__}: {str(error)[:120]})", flush=True)


def ejecutar_lote(pend: list, trabajar, confirmar, max_llamadas: int, hasta_t: float | None = None, n_hilos: int | None = None,
                  reloj=time.monotonic, al_fallar=None) -> dict:
    """Procesa `pend` ([(empresa, meta)]) en el orden dado.
      trabajar(e, m) -> Res | None   corre en un hilo; None = no hay nada que hacer con ese documento (no cuenta ni se confirma)
      confirmar(e, m, res)           corre en el hilo principal, de a uno y EN EL ORDEN DE ENVIO
      al_fallar(e, m, error)         (opcional) para un error inesperado del hilo; sin el, se imprime y el documento queda pendiente
    Devuelve {"corte": None | "max" | "tiempo", "indice": posicion del primer documento NO enviado (o None), "llamadas": cuantos contaron}."""
    global _limitador
    n = n_hilos if n_hilos is not None else hilos()
    llamadas = 0

    def tope() -> bool:
        return llamadas >= max_llamadas

    def vencido() -> bool:
        return bool(hasta_t and reloj() > hasta_t)

    if n <= 1:                                                   # camino secuencial: sin pool, identico al bucle de siempre
        for i, (e, m) in enumerate(pend):
            if tope():
                return {"corte": "max", "indice": i, "llamadas": llamadas}
            if vencido():
                return {"corte": "tiempo", "indice": i, "llamadas": llamadas}
            try:
                res = trabajar(e, m)
            except Exception as error:
                if not es_error_de_cuenta(error):
                    raise
                avisar_cuenta(error)
                return {"corte": "cuenta", "indice": i, "llamadas": llamadas}
            if res is None:
                continue
            confirmar(e, m, res)
            llamadas += bool(res.llamo)
        return {"corte": None, "indice": None, "llamadas": llamadas}

    limitador = Limitador(n)
    _limitador = limitador
    corte, indice = None, None
    try:
        with ThreadPoolExecutor(max_workers=n) as pool:
            orden = deque()                    # (futuro, e, m) en el orden de envio, aun sin confirmar
            siguiente = 0
            while True:
                # 1) enviar mientras haya cupo, tiempo, documentos y margen bajo el tope (los que estan en vuelo cuentan como posibles llamadas)
                while corte is None and siguiente < len(pend) and len(orden) < min(n, limitador.permitidos()):
                    if vencido():
                        corte, indice = "tiempo", siguiente
                        break
                    if llamadas + len(orden) >= max_llamadas:
                        if not orden:
                            corte, indice = "max", siguiente
                        break                  # hay que esperar a ver cuantos de los que vuelan realmente cuentan
                    e, m = pend[siguiente]
                    orden.append((pool.submit(trabajar, e, m), e, m))
                    siguiente += 1
                if not orden:                          # nada en vuelo y nada mas para enviar (se acabaron los documentos o hubo corte)
                    break
                # 2) esperar al primero en orden y confirmarlo (los demas siguen trabajando); los ya terminados siguen despues
                futuro, e, m = orden[0]
                wait([futuro], return_when=FIRST_COMPLETED)
                orden.popleft()
                try:
                    res = futuro.result()
                except Exception as error:     # un fallo inesperado de UN hilo no pierde a los demas
                    if es_error_de_cuenta(error):
                        if corte != "cuenta":
                            avisar_cuenta(error)
                        corte, indice = "cuenta", siguiente        # no se envia nada mas; los que ya vuelan se confirman o descartan normalmente
                        continue
                    if al_fallar:
                        al_fallar(e, m, error)
                    else:
                        print(f"  {e['empresa']} #{m['id']}: error inesperado {type(error).__name__}: {error}", flush=True)
                    continue
                if res is None:
                    continue
                confirmar(e, m, res)
                llamadas += bool(res.llamo)
    finally:
        _limitador = None
    return {"corte": corte, "indice": indice, "llamadas": llamadas, "bajadas_de_concurrencia": limitador.bajadas}
