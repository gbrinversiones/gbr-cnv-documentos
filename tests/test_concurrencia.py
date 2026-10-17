"""Fase 24: llamadas al modelo en paralelo. Con CNV_CONCURRENCIA=1 todo es identico al bucle secuencial (el resto de la suite corre asi, ver conftest)."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import avisos_pago as avp
from cnv import clasificacion as cla
from cnv import concurrencia as conc
from cnv import condiciones as con
from cnv import eventos as eve
from cnv import financieros as fin
from cnv import resumenes as res

from .test_eventos import DIV_OK, DIVIDENDOS
from .test_financieros import BALANCE, respuesta as resp_fin

EMP = {"empresa": "EMPRESA EJEMPLO S.A.", "cuit": "30111111112"}
OTRA = {"empresa": "OTRA S.A.", "cuit": "30500010912"}


# --- configuracion ---------------------------------------------------------------------------

@pytest.mark.parametrize("valor,esperado", [(None, 2), ("1", 1), ("6", 6), ("9", 6), ("0", 1), ("-3", 1), ("abc", 2), ("", 2), ("3", 3), ("4", 4)])
def test_hilos_por_variable_de_entorno(monkeypatch, valor, esperado):
    monkeypatch.delenv("CNV_CONCURRENCIA", raising=False)
    if valor is not None:
        monkeypatch.setenv("CNV_CONCURRENCIA", valor)
    assert conc.hilos() == esperado


# --- reintentos --------------------------------------------------------------------------------

class ErrorApi(Exception):
    def __init__(self, estado=None, retry_after=None, mensaje="x"):
        super().__init__(mensaje)
        self.status_code = estado
        self.response = SimpleNamespace(headers={"retry-after": retry_after} if retry_after is not None else {}) if estado else None


class RateLimitError(Exception):
    pass


@pytest.mark.parametrize("error,reintentable,limite", [
    (ErrorApi(429), True, True), (ErrorApi(529), True, True), (ErrorApi(500), True, False), (ErrorApi(503), True, False), (ErrorApi(408), True, False),
    (ErrorApi(400), False, False), (ErrorApi(401), False, False), (ErrorApi(404), False, False), (RuntimeError("Overloaded, try later"), True, True),
    (RateLimitError("x"), True, True), (ValueError("json"), False, False), (RuntimeError("respuesta incompleta"), False, False)])
def test_clasificacion_de_errores(error, reintentable, limite):
    assert conc.es_reintentable(error) is reintentable and conc.es_limite_de_tasa(error) is limite


def test_espera_respeta_retry_after_y_tiene_tope_y_sino_crece():
    assert conc.espera_para(ErrorApi(429, retry_after="7"), 0) == 7.0
    assert conc.espera_para(ErrorApi(429, retry_after="2.5"), 3) == 2.5
    assert conc.espera_para(ErrorApi(429, retry_after="9999"), 0) == conc.ESPERA_MAXIMA
    azar = lambda: 0.5  # noqa: E731
    assert conc.espera_para(ErrorApi(500), 0, azar) == 2.5 and conc.espera_para(ErrorApi(500), 1, azar) == 4.5 and conc.espera_para(ErrorApi(500), 2, azar) == 8.5
    assert conc.espera_para(ErrorApi(500), 20, azar) == 60.0                                      # tope
    assert conc.espera_para(ErrorApi(429, retry_after="mañana"), 0, azar) == 2.5                  # header ilegible: espera creciente
    assert conc.espera_para(ValueError("sin response"), 0, azar) == 2.5


def test_con_reintentos_se_recupera_de_un_429_esperando_lo_que_pide_la_api():
    esperas, llamadas = [], []

    def fn():
        llamadas.append(1)
        if len(llamadas) <= 2:
            raise ErrorApi(429, retry_after="3")
        return "ok"

    assert conc.con_reintentos(fn, dormir=esperas.append) == "ok" and esperas == [3.0, 3.0] and len(llamadas) == 3


def test_con_reintentos_se_rinde_tras_los_intentos_y_no_reintenta_lo_que_no_corresponde():
    esperas = []
    with pytest.raises(ErrorApi):
        conc.con_reintentos(lambda: (_ for _ in ()).throw(ErrorApi(503)), intentos=3, dormir=esperas.append, azar=lambda: 0)
    assert esperas == [2.0, 4.0]                                                             # 3 intentos = 2 esperas
    esperas.clear()
    with pytest.raises(ErrorApi):
        conc.con_reintentos(lambda: (_ for _ in ()).throw(ErrorApi(400)), dormir=esperas.append)
    assert esperas == []


def test_crear_mensaje_pasa_por_los_reintentos(monkeypatch):
    monkeypatch.setattr(conc.time, "sleep", lambda s: None)
    intentos = []

    def crear(**kw):
        intentos.append(kw)
        if len(intentos) == 1:
            raise ErrorApi(529, mensaje="Overloaded")
        return "respuesta"

    assert conc.crear_mensaje(SimpleNamespace(messages=SimpleNamespace(create=crear)), model="m", max_tokens=5) == "respuesta" and len(intentos) == 2 and intentos[0] == {"model": "m", "max_tokens": 5}


# --- limitador ------------------------------------------------------------------------------------

def test_limitador_baja_con_limites_repetidos_y_sube_de_a_uno():
    t = [0.0]
    lim = conc.Limitador(4, reloj=lambda: t[0], ventana=60, umbral=3, exitos_para_subir=2)
    lim.limite()
    lim.limite()
    assert lim.permitidos() == 4                      # dos limites no alcanzan
    lim.limite()
    assert lim.permitidos() == 2 and lim.bajadas == 1
    for _ in range(3):
        lim.limite()
    assert lim.permitidos() == 1                      # nunca baja de 1
    for _ in range(3):
        lim.limite()
    assert lim.permitidos() == 1
    lim.exito()
    assert lim.permitidos() == 1
    lim.exito()
    assert lim.permitidos() == 2
    for _ in range(20):
        lim.exito()
    assert lim.permitidos() == 4                      # y no pasa del maximo


def test_limitador_olvida_los_limites_viejos_y_un_exito_corta_la_racha_de_limites():
    t = [0.0]
    lim = conc.Limitador(4, reloj=lambda: t[0], ventana=60, umbral=3)
    lim.limite()
    lim.limite()
    t[0] = 120.0
    lim.limite()
    assert lim.permitidos() == 4                      # los dos primeros salieron de la ventana


# --- ejecutor -----------------------------------------------------------------------------------------

def docs(n):
    return [({"empresa": "E", "cuit": "1"}, {"id": str(i)}) for i in range(1, n + 1)]


def correr(pend, trabajar, max_llamadas=1000, hasta_t=None, n=4, **kw):
    confirmados = []
    r = conc.ejecutar_lote(pend, trabajar, lambda e, m, res: confirmados.append((m["id"], res.datos, threading.current_thread() is threading.main_thread())), max_llamadas, hasta_t, n, **kw)
    return r, confirmados


def test_se_confirma_en_orden_de_envio_aunque_los_hilos_terminen_desordenados():
    def trabajar(e, m):
        time.sleep(0.03 * (6 - int(m["id"])))          # el 1 es el mas lento
        return conc.Res(m["id"])

    r, conf = correr(docs(5), trabajar)
    assert [c[0] for c in conf] == ["1", "2", "3", "4", "5"] and all(c[2] for c in conf) and r["corte"] is None and r["llamadas"] == 5      # y se confirma en el hilo principal


def test_realmente_corre_en_paralelo():
    barrera = threading.Barrier(3, timeout=5)

    def trabajar(e, m):
        barrera.wait()                                  # solo pasa si hay 3 hilos trabajando a la vez
        return conc.Res(m["id"])

    r, conf = correr(docs(3), trabajar, n=3)
    assert [c[0] for c in conf] == ["1", "2", "3"]


def test_con_un_hilo_el_trabajo_corre_en_el_hilo_principal_y_en_orden():
    hilos_usados = []

    def trabajar(e, m):
        hilos_usados.append(threading.current_thread() is threading.main_thread())
        return conc.Res(m["id"])

    r, conf = correr(docs(4), trabajar, n=1)
    assert all(hilos_usados) and [c[0] for c in conf] == ["1", "2", "3", "4"]


def test_el_tope_no_se_pasa_nunca_y_lo_no_enviado_queda_pendiente():
    enviados = []

    def trabajar(e, m):
        enviados.append(m["id"])
        time.sleep(0.01)
        return conc.Res(m["id"])

    r, conf = correr(docs(10), trabajar, max_llamadas=5)
    assert len(enviados) == 5 and len(conf) == 5 and r == {"corte": "max", "indice": 5, "llamadas": 5, "bajadas_de_concurrencia": 0}
    r1, conf1 = correr(docs(10), trabajar, max_llamadas=5, n=1)
    assert len(conf1) == 5 and (r1["corte"], r1["indice"]) == ("max", 5)             # igual que en secuencial


def test_lo_que_no_cuenta_no_gasta_el_tope():
    def trabajar(e, m):
        return conc.Res(m["id"], llamo=int(m["id"]) % 2 == 0)             # los impares no llaman al modelo (p. ej. escaneados)

    r, conf = correr(docs(10), trabajar, max_llamadas=3)
    assert r["llamadas"] == 3 and (r["corte"], r["indice"]) == ("max", 6) and [c[0] for c in conf] == ["1", "2", "3", "4", "5", "6"]
    r1, conf1 = correr(docs(10), trabajar, max_llamadas=3, n=1)
    assert [c[0] for c in conf1] == [c[0] for c in conf] and r1["indice"] == r["indice"]          # el mismo corte que en secuencial


def test_los_documentos_sin_nada_que_hacer_se_saltean():
    r, conf = correr(docs(6), lambda e, m: None if int(m["id"]) % 2 else conc.Res(m["id"]))
    assert [c[0] for c in conf] == ["2", "4", "6"] and r["llamadas"] == 3 and r["corte"] is None


def test_corte_por_tiempo_deja_de_enviar_pero_espera_y_confirma_a_los_que_estan_en_vuelo():
    reloj_t = [0.0]
    iniciados = threading.Semaphore(0)
    liberar = threading.Event()

    def trabajar(e, m):
        iniciados.release()
        liberar.wait(5)                                 # siguen "en vuelo" hasta que el reloj ya paso del limite
        return conc.Res(m["id"])

    def pasar_el_tiempo():
        for _ in range(3):
            iniciados.acquire(timeout=5)                # los 3 primeros ya salieron
        reloj_t[0] = 100.0                              # vence el tiempo con 3 en vuelo
        liberar.set()

    threading.Thread(target=pasar_el_tiempo).start()
    r, conf = correr(docs(10), trabajar, hasta_t=50.0, n=3, reloj=lambda: reloj_t[0])
    assert [c[0] for c in conf] == ["1", "2", "3"] and r["corte"] == "tiempo" and r["indice"] == 3 and r["llamadas"] == 3          # no se perdio ninguno


def test_corte_por_tiempo_en_secuencial_no_envia_nada_si_ya_vencio():
    r, conf = correr(docs(3), lambda e, m: conc.Res(1), hasta_t=1.0, n=1, reloj=lambda: 2.0)
    assert conf == [] and (r["corte"], r["indice"]) == ("tiempo", 0)


def test_un_hilo_que_falla_no_pierde_al_resto(capsys):
    def trabajar(e, m):
        if m["id"] == "3":
            raise ZeroDivisionError("bug")
        return conc.Res(m["id"])

    r, conf = correr(docs(6), trabajar)
    assert [c[0] for c in conf] == ["1", "2", "4", "5", "6"] and "#3: error inesperado ZeroDivisionError" in capsys.readouterr().out
    fallos = []
    r, conf = correr(docs(6), trabajar, al_fallar=lambda e, m, error: fallos.append((m["id"], type(error).__name__)))
    assert fallos == [("3", "ZeroDivisionError")] and len(conf) == 5


def test_el_limitador_baja_la_concurrencia_con_429_sostenidos(monkeypatch):
    monkeypatch.setattr(conc.time, "sleep", lambda s: None)

    def trabajar(e, m):
        intentos = [0]

        def crear(**kw):                               # el primer pedido de CADA documento recibe un 429 (con retry-after 0); el segundo pasa
            intentos[0] += 1
            if intentos[0] == 1:
                raise ErrorApi(429, retry_after="0")
            return "ok"

        return conc.Res(conc.crear_mensaje(SimpleNamespace(messages=SimpleNamespace(create=crear))))

    r, conf = correr(docs(30), trabajar, n=6)
    assert len(conf) == 30 and r["bajadas_de_concurrencia"] >= 1 and conc._limitador is None          # todos terminaron (con reintentos) y la concurrencia bajo sola


def test_cuando_la_api_se_recupera_la_concurrencia_vuelve_a_subir():
    lim = conc.Limitador(6, umbral=2, exitos_para_subir=3)
    lim.limite()
    lim.limite()
    assert lim.permitidos() == 3
    for _ in range(9):
        lim.exito()
    assert lim.permitidos() == 6


# --- modulos: mismo resultado en serie y en paralelo ---------------------------------------------------

def montar_hechos(tmp_path, n=12, k=None):
    a = alm.AlmacenLocal(str(tmp_path))
    ind = [{"id": str(100 + i), "t": "hechos", "f": f"2026-10-{1 + i % 28:02d}", "d": f"hecho {100 + i}", "texto_ok": True, "u": f"http://x/{100 + i}", **({"k": k} if k else {})} for i in range(n)]
    alm.guardar_indice_empresa(a, EMP["cuit"], ind)
    for m in ind:
        alm.guardar_texto(a, EMP["cuit"], m["id"], DIVIDENDOS)
    return a


class ClienteRapido:
    """Doble thread-safe cuya respuesta depende SOLO del documento (no del orden de las llamadas)."""
    def __init__(self, respuesta_de, demora=0.003):
        self.respuesta_de, self.demora, self.llamadas, self.cerrojo = respuesta_de, demora, 0, threading.Lock()
        self.messages = SimpleNamespace(create=self._crear)

    def _crear(self, **kw):
        with self.cerrojo:
            self.llamadas += 1
        time.sleep(self.demora)
        contenido = kw["messages"][0]["content"]
        x = self.respuesta_de(contenido)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=x if isinstance(x, str) else json.dumps(x))])


def id_de(contenido):
    return int(contenido.split("hecho ")[1].split("\n")[0].split()[0]) if "hecho " in contenido else 0


def bytes_de(a, nombres):
    return {n: a.leer(n) for n in nombres}


def test_resumenes_en_paralelo_da_lo_mismo_que_en_serie(tmp_path):
    cliente = lambda: ClienteRapido(lambda c: f"Resumen del hecho {id_de(c)}.")  # noqa: E731
    a1, a4 = montar_hechos(tmp_path / "s"), montar_hechos(tmp_path / "p")
    t1 = res.ejecutar(a1, [EMP], cliente(), 100, n_hilos=1)
    c4 = cliente()
    t4 = res.ejecutar(a4, [EMP], c4, 100, n_hilos=4)
    assert t1 == t4 == {"resumidos": 12, "errores": 0, "pendientes": 0} and c4.llamadas == 12
    assert a1.leer("cnv/indice/30111111112.json") == a4.leer("cnv/indice/30111111112.json")
    assert res.ejecutar(a4, [EMP], c4, 100, n_hilos=4)["resumidos"] == 0 and c4.llamadas == 12          # segunda pasada: idempotente, ni una llamada


def test_resumenes_el_tope_y_los_errores_en_paralelo(tmp_path):
    def resp(c):
        if id_de(c) % 4 == 0:
            raise RuntimeError("api")
        return f"Resumen {id_de(c)}"

    class Cliente(ClienteRapido):
        def _crear(self, **kw):
            if id_de(kw["messages"][0]["content"]) % 4 == 0:
                raise RuntimeError("api")
            return super()._crear(**kw)

    a = montar_hechos(tmp_path)
    t = res.ejecutar(a, [EMP], Cliente(resp), 8, n_hilos=4)
    assert t["resumidos"] + t["errores"] == 8 and t["pendientes"] == 4 and t["errores"] == 2      # el tope cuenta intentos, como antes


def test_clasificar_en_paralelo_da_lo_mismo_que_en_serie_y_cuenta_los_reintentos(tmp_path):
    def resp(c):
        n = id_de(c)
        return {"categoria": "dividendos" if n % 2 else "otro", "cita": "dividendo en efectivo" if n % 3 else "esta cita no existe en el documento"}

    cliente = lambda: ClienteRapido(resp)  # noqa: E731
    a1, a4 = montar_hechos(tmp_path / "s"), montar_hechos(tmp_path / "p")
    t1 = cla.ejecutar(a1, [EMP], cliente(), 100, n_hilos=1)
    t4 = cla.ejecutar(a4, [EMP], cliente(), 100, n_hilos=4)
    assert t1 == t4 and t4["reintentos"] > 0 and t4["clasificados"] + t4["descartados"] == 12
    assert a1.leer("cnv/indice/30111111112.json") == a4.leer("cnv/indice/30111111112.json")
    c = cliente()
    assert cla.ejecutar(a4, [EMP], c, 100, n_hilos=4)["clasificados"] == 0 or c.llamadas <= 12      # los descartados reintentan segun su contador; nada nuevo se clasifica de mas


def test_eventos_en_paralelo_da_lo_mismo_que_en_serie_e_idempotente(tmp_path):
    cliente = lambda: ClienteRapido(lambda c: {"es_evento": True, "datos": DIV_OK})  # noqa: E731
    a1, a4 = montar_hechos(tmp_path / "s", k="dividendos"), montar_hechos(tmp_path / "p", k="dividendos")
    t1 = eve.ejecutar(a1, [EMP], cliente(), 100, n_hilos=1)
    c4 = cliente()
    t4 = eve.ejecutar(a4, [EMP], c4, 100, n_hilos=4)
    assert t1 == t4 and t4["ok"] == 12 and c4.llamadas == 12
    assert a1.leer("cnv/eventos/30111111112.json") == a4.leer("cnv/eventos/30111111112.json")
    assert eve.ejecutar(a4, [EMP], c4, 100, n_hilos=4)["procesados"] == 0 and c4.llamadas == 12


def test_financieros_avisos_y_condiciones_en_paralelo_dan_lo_mismo_que_en_serie(tmp_path):
    def montar(raiz):
        a = alm.AlmacenLocal(str(raiz))
        ind = []
        for i in range(8):
            ind.append({"id": str(200 + i), "t": "estados_contables", "f": f"2026-09-{1 + i:02d}", "texto_ok": True, "d": "TIPO BALANCE: CONSOLIDADO"})
            ind.append({"id": str(300 + i), "t": "avisos_pago", "f": f"2026-09-{1 + i:02d}", "texto_ok": True})
            ind.append({"id": str(400 + i), "t": "prospectos", "f": f"2026-09-{1 + i:02d}", "texto_ok": True, "s": "Suplemento"})
        ind.append({"id": "250", "t": "estados_contables", "f": "2026-09-20", "texto_ok": True, "d": "CUIT: 30-99999999-1 TIPO BALANCE: CONSOLIDADO"})      # de otra entidad
        alm.guardar_indice_empresa(a, EMP["cuit"], ind)
        for m in ind:
            alm.guardar_texto(a, EMP["cuit"], m["id"], BALANCE if m["t"] == "estados_contables" else "[pagina 1]\n" + "texto sin datos utiles " * 30)
        return a

    cliente_f = lambda: ClienteRapido(lambda c: resp_fin())  # noqa: E731
    cliente_x = lambda: ClienteRapido(lambda c: {"es_pago": False, "pagos": [], "es_emision_on": False, "datos": [], "emisor": ""})  # noqa: E731
    a1, a4 = montar(tmp_path / "s"), montar(tmp_path / "p")
    f1, f4 = fin.ejecutar(a1, [EMP], cliente_f(), 100, n_hilos=1), fin.ejecutar(a4, [EMP], cliente_f(), 100, n_hilos=4)
    assert f1 == f4 and f4["ok"] == 8 and f4["saltados"] == 1
    v1, v4 = avp.ejecutar(a1, [EMP], cliente_x(), 100, n_hilos=1), avp.ejecutar(a4, [EMP], cliente_x(), 100, n_hilos=4)
    c1, c4 = con.ejecutar(a1, [EMP], cliente_x(), 100, n_hilos=1), con.ejecutar(a4, [EMP], cliente_x(), 100, n_hilos=4)
    assert v1 == v4 and c1 == c4
    for n in ("financieros", "avisos_pago", "condiciones"):
        assert a1.leer(f"cnv/{n}/30111111112.json") == a4.leer(f"cnv/{n}/30111111112.json"), n
    assert fin.ejecutar(a4, [EMP], cliente_f(), 100, n_hilos=4)["procesados"] == 0          # segunda pasada


def test_corte_por_tiempo_en_un_modulo_guarda_todo_lo_que_estaba_en_vuelo(tmp_path):
    a = montar_hechos(tmp_path, n=30, k="dividendos")
    cliente = ClienteRapido(lambda c: {"es_evento": True, "datos": DIV_OK}, demora=0.05)
    t = eve.ejecutar(a, [EMP], cliente, 1000, hasta_t=time.monotonic() + 0.12, n_hilos=4)
    guardados = len(eve.leer_eventos(a, EMP["cuit"])["procesados"])
    assert 0 < guardados < 30 and guardados == t["procesados"] == cliente.llamadas and t["pendientes"] == 30 - guardados      # ninguna llamada pagada se perdio
    t2 = eve.ejecutar(a, [EMP], cliente, 1000, n_hilos=4)
    assert t2["procesados"] == 30 - guardados and len(eve.leer_eventos(a, EMP["cuit"])["procesados"]) == 30


def test_con_concurrencia_1_los_modulos_corren_en_el_hilo_principal(tmp_path):
    hilos_usados = []

    def resp(c):
        hilos_usados.append(threading.current_thread() is threading.main_thread())
        return "Resumen."

    res.ejecutar(montar_hechos(tmp_path), [EMP], ClienteRapido(resp, demora=0), 100)           # sin n_hilos: lo da CNV_CONCURRENCIA=1 (conftest)
    assert hilos_usados and all(hilos_usados)


def test_un_error_del_modelo_en_un_documento_no_frena_a_los_demas_en_paralelo(tmp_path):
    class Cliente(ClienteRapido):
        def _crear(self, **kw):
            if id_de(kw["messages"][0]["content"]) == 105:
                raise ErrorApi(401)
            return super()._crear(**kw)

    a = montar_hechos(tmp_path)
    t = res.ejecutar(a, [EMP], Cliente(lambda c: "Resumen."), 100, n_hilos=4)
    assert t["resumidos"] == 11 and t["errores"] == 1


# --- CLI ---------------------------------------------------------------------------------------------------

def test_los_clientes_de_la_api_se_crean_sin_reintentos_propios(tmp_path, monkeypatch, capsys):
    """Los reintentos los hace cnv/concurrencia.py (con retry-after y limitador); el SDK no debe reintentar por su cuenta (se sumarian)."""
    creados = []
    monkeypatch.setitem(__import__("sys").modules, "anthropic", SimpleNamespace(Anthropic=lambda **k: creados.append(k) or ClienteRapido(lambda c: "Resumen.", 0)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP])
    for cmd in (["resumenes"], ["clasificar"], ["eventos"], ["avisos-pago"], ["condiciones"], ["financieros"]):
        assert cli.main(cmd) == 0, cmd
    assert len(creados) == 6 and all(k.get("max_retries") == 0 and "timeout" in k for k in creados)


# --- presupuesto proactivo de tokens por minuto (CNV_TPM) -----------------------------------------

class RelojFalso:
    def __init__(self):
        self.t, self.dormido = 1000.0, []

    def __call__(self):
        return self.t

    def dormir(self, s):
        self.dormido.append(s)
        self.t += s


def presupuesto_falso(limite):
    r = RelojFalso()
    return conc.Presupuesto(limite, reloj=r, dormir=r.dormir), r


@pytest.mark.parametrize("valor,esperado", [(None, 150_000), ("100000", 100_000), ("abc", 150_000), ("0", 150_000), ("-5", 150_000), ("", 150_000)])
def test_tpm_por_variable_de_entorno(monkeypatch, valor, esperado):
    monkeypatch.delenv("CNV_TPM", raising=False)
    if valor is not None:
        monkeypatch.setenv("CNV_TPM", valor)
    assert conc.tpm() == esperado


def test_estimacion_de_tokens_es_caracteres_sobre_3_redondeado_hacia_arriba():
    assert conc.estimar_tokens({"system": "a" * 30, "messages": [{"role": "user", "content": "b" * 60}]}) == 30
    assert conc.estimar_tokens({"system": "a", "messages": [{"role": "user", "content": "b"}]}) == 1                # 2 caracteres -> 1 token, no 0
    assert conc.estimar_tokens({"system": "a" * 10, "messages": [{"role": "user", "content": "b" * 10}]}) == 7        # 20 / 3 = 6,67 -> 7
    assert conc.estimar_tokens({}) == 1 and conc.estimar_tokens({"messages": [{"content": [{"type": "text"}]}]}) >= 1


def test_el_presupuesto_deja_pasar_lo_que_entra_sin_esperar():
    p, r = presupuesto_falso(1000)
    assert p.esperar(400) == 0 and p.esperar(600) == 0 and r.dormido == [] and p.usado() == 1000


def test_el_presupuesto_espera_hasta_que_salga_lo_viejo_de_la_ventana():
    p, r = presupuesto_falso(1000)
    p.esperar(600)                                  # t = 1000
    r.t += 20
    p.esperar(400)                                  # t = 1020: llenan los 1000
    espera = p.esperar(500)                         # no entra: tiene que salir lo de t = 1000 (a los 60 s)
    assert espera == pytest.approx(40.0) and r.t == pytest.approx(1060.0) and p.usado() == 900          # 400 (t = 1020) + 500 (t = 1060)
    assert p.esperas == 1 and p.esperado == pytest.approx(40.0)


def test_el_acumulado_de_cualquier_ventana_de_60_s_nunca_supera_el_limite():
    p, r = presupuesto_falso(1000)
    eventos = []
    for n in (300, 700, 450, 450, 100, 900, 250, 250, 250, 250, 600, 600):
        p.esperar(n)
        eventos.append((r.t, n))
    for i, (t0, _) in enumerate(eventos):
        assert sum(n for t, n in eventos if t0 <= t < t0 + 60) <= 1000, i


def test_un_pedido_mas_grande_que_todo_el_presupuesto_pasa_cuando_la_ventana_esta_vacia():
    p, r = presupuesto_falso(1000)
    assert p.esperar(5000) == 0                      # ventana vacia: pasa (si no, esperaria para siempre)
    espera = p.esperar(10)                           # pero ocupa la ventana: el siguiente espera a que se libere
    assert espera == pytest.approx(60.0)


def test_el_presupuesto_se_comparte_entre_hilos_y_no_se_pasa(monkeypatch):
    p = conc.Presupuesto(1000, ventana=0.2)          # ventana corta real para no esperar de verdad
    registrados, cerrojo = [], threading.Lock()

    def trabajar(e, m):
        p.esperar(400)
        with cerrojo:
            registrados.append(time.monotonic())
        return conc.Res(m["id"])

    r, conf = correr(docs(8), trabajar, n=4)
    assert len(conf) == 8 and p.esperas > 0
    for t0 in registrados:                           # en cualquier ventana de 0,2 s hay a lo sumo 2 pedidos de 400 (800 <= 1000)
        assert sum(1 for t in registrados if t0 <= t < t0 + 0.2) <= 2


def test_crear_mensaje_reserva_los_tokens_estimados_antes_de_llamar(monkeypatch):
    p, r = presupuesto_falso(100)
    monkeypatch.setattr(conc, "_presupuesto", p)
    eventos = []
    cliente = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: eventos.append(("llamada", r.t)) or "ok"))
    kw = dict(model="m", system="s" * 150, messages=[{"role": "user", "content": "u" * 150}])        # 300 caracteres = 100 tokens: llena la ventana
    conc.crear_mensaje(cliente, **kw)
    conc.crear_mensaje(cliente, **kw)
    assert [t for _, t in eventos] == [1000.0, 1060.0]          # el segundo pedido espero a que la ventana se liberara


def test_cada_reintento_vuelve_a_reservar_tokens(monkeypatch):
    p, r = presupuesto_falso(100)
    monkeypatch.setattr(conc, "_presupuesto", p)
    monkeypatch.setattr(conc.time, "sleep", lambda s: None)
    llamadas = []

    def crear(**kw):
        llamadas.append(r.t)
        if len(llamadas) == 1:
            raise ErrorApi(429, retry_after="0")
        return "ok"

    conc.crear_mensaje(SimpleNamespace(messages=SimpleNamespace(create=crear)), system="s" * 300)          # 100 tokens por intento
    assert llamadas == [1000.0, 1060.0]


def test_el_presupuesto_del_proceso_toma_cnv_tpm(monkeypatch):
    monkeypatch.setattr(conc, "_presupuesto", None)
    monkeypatch.setenv("CNV_TPM", "12345")
    assert conc.presupuesto().limite == 12345 and conc.presupuesto() is conc.presupuesto()


def test_el_modulo_con_modelo_pasa_por_el_presupuesto(tmp_path, monkeypatch):
    p, r = presupuesto_falso(10 ** 9)
    monkeypatch.setattr(conc, "_presupuesto", p)
    a = montar_hechos(tmp_path, n=3)
    res.ejecutar(a, [EMP], ClienteRapido(lambda c: "Resumen.", 0), 100, n_hilos=1)
    assert p.usado() > 0 and len(p.usos) == 3                    # tres pedidos reservaron tokens
