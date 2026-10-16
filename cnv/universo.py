"""Control del universo de empresas (data/universo.csv) contra la CNV. NUNCA modifica el CSV: solo genera un informe.

--verificar : para cada fila abre la pagina publica de la empresa en la CNV y compara la razon social que muestra
              ("Resultados de busqueda: <razon social> ...") con la del CSV. Detecta CUIT que apuntan a otra empresa,
              CUIT inexistentes, CUIT con digito verificador invalido y duplicados.
--sugerir   : consulta el autocompletado publico de la CNV (Empresas/Autocomplete?term=...) y lista candidatos
              con su CUIT, para decidir a mano si se agregan al CSV.

Como la CNV es lenta y se corta: reintentos con espera creciente, tope de tiempo y un archivo de avance (el propio informe
JSON): una corrida cortada se retoma sin repetir las filas ya verificadas.
"""
import datetime as dt
import json
import os
import re
import time
import unicodedata

from .listado import URL_LISTADO, sesion
from .salud import cuit_valido

URL_AUTOCOMPLETADO = "https://www.cnv.gov.ar/SitioWeb/Empresas/Autocomplete"
# la razon social va entre el rotulo y la primera marca de estado de la pagina; "Sin" solo cuenta como palabra entera
_RE_RAZON = re.compile(r"(?i:Resultados de b[uú]squeda:)\s*(.+?)\s+(?:En Oferta|Sin\b|Retirad|ALYC|CUIT)")
_RE_MARCADOR = re.compile(r"Resultados de b[uú]squeda:", re.I)
_SUFIJOS = {"sa", "s", "a", "sau", "srl", "saic", "saica", "sacifa", "ciasa", "c", "i", "u", "y", "e", "de", "del", "la", "el", "los", "las", "sociedad", "anonima",
            "sucursal", "argentina", "argentino", "holding", "grupo", "cia", "co", "sc", "scpa", "sacif", "sapem", "saci", "sh", "ltd", "inc", "llc"}


def normalizar(s: str) -> str:
    t = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def palabras_clave(nombre: str) -> list:
    """Palabras que identifican a la empresa (sin acentos, sin sufijos societarios)."""
    return [w for w in re.split(r"[^a-z0-9]+", normalizar(nombre)) if w and w not in _SUFIJOS]


def comparar(nombre_csv: str, razon_cnv: str) -> str:
    """"coincide" (las palabras clave de uno estan todas en el otro), "parcial" (comparten alguna) o "distinta"."""
    a, b = set(palabras_clave(nombre_csv)), set(palabras_clave(razon_cnv))
    if not a or not b:
        return "distinta"
    if a <= b or b <= a:
        return "coincide"
    return "parcial" if a & b else "distinta"


def _texto_plano(html: str) -> str:
    sin_codigo = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return " ".join(re.sub(r"<[^>]+>", " ", sin_codigo).split())


def razon_social_de_pagina(html: str) -> tuple:
    """(estado, razon_social | None, fragmento). "ok" si la pagina muestra "Resultados de busqueda: <razon>",
    "sin_razon" si hay marcador pero no se pudo aislar el nombre, "sin_marcador" si es la portada generica (CUIT inexistente)."""
    texto = _texto_plano(html)
    m = _RE_RAZON.search(texto)
    if m:
        return "ok", m.group(1).strip(), texto[m.start():m.start() + 160]
    mm = _RE_MARCADOR.search(texto)
    if mm:
        return "sin_razon", None, texto[mm.start():mm.start() + 160]
    return "sin_marcador", None, ""


def _con_reintentos(fn, intentos: int, espera: float, dormir, hasta_t: float | None):
    ultimo = None
    for n in range(intentos):
        try:
            return fn()
        except Exception as error:  # la CNV responde lento o corta
            ultimo = error
            pausa = espera * (n + 1)
            if n == intentos - 1 or (hasta_t is not None and time.monotonic() + pausa > hasta_t):
                break
            dormir(pausa)
    raise ultimo


def consultar_cuit(cuit: str, s=None, intentos: int = 3, espera: float = 4.0, dormir=None, hasta_t: float | None = None) -> dict:
    """{"estado": ok|sin_razon|sin_marcador|error, "razon_social", "fragmento", "error"} de la pagina de la CNV."""
    s = s or sesion()
    dormir = dormir or time.sleep

    def bajar():
        desde = (dt.date.today() - dt.timedelta(days=30)).strftime("%d/%m/%Y")  # ventana corta: solo importa la cabecera con la razon social
        r = s.get(URL_LISTADO.format(cuit=cuit, desde=desde), timeout=60)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.text

    try:
        html = _con_reintentos(bajar, intentos, espera, dormir, hasta_t)
    except Exception as error:
        return {"estado": "error", "razon_social": None, "fragmento": "", "error": f"{type(error).__name__}: {error}"[:120]}
    estado, razon, fragmento = razon_social_de_pagina(html)
    return {"estado": estado, "razon_social": razon, "fragmento": fragmento, "error": None}


def veredicto(fila: dict, consulta: dict) -> str:
    if consulta["estado"] == "error":
        return "error_de_red"
    if consulta["estado"] == "sin_marcador":
        return "cuit_no_existe_en_cnv"
    if consulta["estado"] == "sin_razon":
        return "sin_razon_social_legible"
    return {"coincide": "coincide", "parcial": "revisar_nombre_parcial", "distinta": "razon_social_distinta"}[comparar(fila["empresa"], consulta["razon_social"])]


# ---------------------------------------------------------------------------
# --verificar
# ---------------------------------------------------------------------------

def _cargar_avance(ruta: str | None) -> dict:
    if not ruta or not os.path.exists(ruta):
        return {}
    try:
        with open(ruta, encoding="utf-8") as f:
            return {r["cuit"]: r for r in json.load(f).get("filas", []) if r.get("veredicto") not in (None, "error_de_red")}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}  # avance ilegible: se rehace, no se rompe


def _guardar_informe(ruta: str | None, informe: dict) -> None:
    if not ruta:
        return
    carpeta = os.path.dirname(ruta)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(informe, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ruta)  # escritura atomica: un corte no deja un informe a medias


def verificar(filas: list, s=None, ruta_avance: str | None = None, hasta_t: float | None = None, refrescar: bool = False,
              pausa: float = 0.5, dormir=None, consultar=consultar_cuit) -> dict:
    """Informe {"fecha","filas","resumen","duplicados","pendientes"}. `filas`: [{"empresa","cuit"}] (con o sin CUIT).
    Reutiliza lo ya verificado en `ruta_avance` salvo `refrescar`; guarda el informe tras cada fila."""
    previo = {} if refrescar else _cargar_avance(ruta_avance)
    s = s or sesion()
    dormir = dormir or time.sleep
    resultados, pendientes = [], 0
    cuits = [f["cuit"] for f in filas if f["cuit"]]
    duplicados = sorted({c for c in cuits if cuits.count(c) > 1})
    nombres = [normalizar(f["empresa"]) for f in filas]
    nombres_repetidos = sorted({n for n in nombres if nombres.count(n) > 1})

    def armar():
        return {"fecha": dt.date.today().isoformat(), "filas": resultados, "resumen": _resumen(resultados), "duplicados": duplicados,
                "nombres_repetidos": nombres_repetidos, "pendientes": pendientes}

    for i, f in enumerate(filas):
        base = {"empresa": f["empresa"], "cuit": f["cuit"]}
        if not f["cuit"]:
            resultados.append({**base, "veredicto": "sin_cuit"})
        elif not cuit_valido(f["cuit"]):
            resultados.append({**base, "veredicto": "cuit_invalido"})  # no se gasta una consulta: el digito verificador no cierra
        elif f["cuit"] in previo:
            resultados.append(previo[f["cuit"]] | base)
        elif hasta_t and time.monotonic() > hasta_t:
            pendientes += 1
            resultados.append({**base, "veredicto": "pendiente"})
        else:
            c = consultar(f["cuit"], s, dormir=dormir, hasta_t=hasta_t)
            resultados.append({**base, "razon_social_cnv": c["razon_social"], "fragmento": c["fragmento"], "error": c["error"], "veredicto": veredicto(f, c)})
            dormir(pausa)
            _guardar_informe(ruta_avance, armar())
    informe = armar()
    _guardar_informe(ruta_avance, informe)
    return informe


def _resumen(resultados: list) -> dict:
    cuenta = {}
    for r in resultados:
        cuenta[r["veredicto"]] = cuenta.get(r["veredicto"], 0) + 1
    return cuenta


# ---------------------------------------------------------------------------
# --sugerir
# ---------------------------------------------------------------------------

def buscar_autocompletado(termino: str, s=None, intentos: int = 3, espera: float = 4.0, dormir=None) -> list:
    """[{"cuit","descripcion"}] del autocompletado publico. Lanza si la CNV no responde tras los reintentos."""
    s = s or sesion()
    dormir = dormir or time.sleep

    def pedir():
        r = s.get(URL_AUTOCOMPLETADO, params={"term": termino}, timeout=40)
        r.raise_for_status()
        datos = r.json()
        if not isinstance(datos, list):
            raise ValueError("respuesta inesperada del autocompletado")
        return datos

    return _con_reintentos(pedir, intentos, espera, dormir, None)


def sugerir(nombre: str, universo: list, s=None, buscar=buscar_autocompletado, confirmar: bool = False,
            consultar=consultar_cuit, dormir=None, pausa: float = 0.5) -> dict:
    """Candidatos para `nombre`: prueba el nombre completo y luego versiones cada vez mas cortas (sus palabras clave),
    junta los resultados sin duplicar, los ordena por parecido y marca los que ya estan en el CSV. No decide nada."""
    dormir = dormir or time.sleep
    claves = palabras_clave(nombre)
    terminos = [nombre.strip()] + [" ".join(claves[:n]) for n in range(len(claves), 0, -1)]
    terminos = list(dict.fromkeys(t for t in terminos if t))
    en_csv = {f["cuit"]: f["empresa"] for f in universo if f["cuit"]}
    vistos, candidatos, errores = {}, [], []
    for t in terminos:
        try:
            resultados = buscar(t, s)
        except Exception as error:
            errores.append(f"{t!r}: {type(error).__name__}: {error}"[:140])
            continue
        for x in resultados:
            cuit = re.sub(r"\D", "", str(x.get("cuit", "")))
            if len(cuit) != 11 or cuit in vistos:
                continue
            vistos[cuit] = {"cuit": cuit, "descripcion": " ".join(str(x.get("descripcion", "")).split()), "encontrado_con": t}
        dormir(pausa)
    for c in vistos.values():
        c["parecido"] = comparar(nombre, c["descripcion"])
        c["cuit_valido"] = cuit_valido(c["cuit"])
        c["ya_esta_en_universo"] = en_csv.get(c["cuit"])
        if confirmar:
            consulta = consultar(c["cuit"], s, dormir=dormir)
            c["pagina_cnv"] = consulta["razon_social"] if consulta["estado"] == "ok" else consulta["estado"]
            dormir(pausa)
        candidatos.append(c)
    orden = {"coincide": 0, "parcial": 1, "distinta": 2}
    candidatos.sort(key=lambda c: (orden[c["parecido"]], c["descripcion"]))
    return {"consulta": nombre, "terminos_probados": terminos, "candidatos": candidatos, "errores": errores}


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------

def a_markdown_verificacion(inf: dict) -> str:
    r = inf["resumen"]
    L = [f"## Verificación del universo contra la CNV ({inf['fecha']})", "", "| Veredicto | Filas |", "|---|--:|"]
    L += [f"| {v} | {n} |" for v, n in sorted(r.items(), key=lambda x: -x[1])]
    problemas = [f for f in inf["filas"] if f["veredicto"] not in ("coincide",)]
    L += ["", f"### A revisar a mano ({len(problemas)})"]
    for f in problemas:
        detalle = f" — la CNV dice «{f['razon_social_cnv']}»" if f.get("razon_social_cnv") else f" — {f['error']}" if f.get("error") else ""
        L.append(f"- **{f['veredicto']}**: {f['empresa']} ({f['cuit'] or 'sin CUIT'}){detalle}")
    if not problemas:
        L.append("- nada: todas las filas coinciden")
    if inf["duplicados"]:
        L += ["", f"CUIT duplicados en el CSV: {', '.join(inf['duplicados'])}"]
    if inf["nombres_repetidos"]:
        L += ["", f"Razones sociales repetidas en el CSV: {', '.join(inf['nombres_repetidos'])}"]
    if inf["pendientes"]:
        L += ["", f"**{inf['pendientes']} filas quedaron pendientes por el tope de tiempo**: volver a correr con el mismo `--salida` retoma desde ahí."]
    L += ["", "_El CSV no se modificó. Revisar cada caso antes de tocarlo._", ""]
    return "\n".join(L)


def a_markdown_sugerencias(inf: dict) -> str:
    L = [f"### Candidatos para «{inf['consulta']}»", ""]
    if not inf["candidatos"]:
        L.append("- ninguno (probado con: " + ", ".join(repr(t) for t in inf["terminos_probados"]) + ")")
    for c in inf["candidatos"]:
        marcas = [c["parecido"]] + (["CUIT inválido"] if not c["cuit_valido"] else []) + ([f"YA ESTÁ en el CSV como «{c['ya_esta_en_universo']}»"] if c["ya_esta_en_universo"] else [])
        pagina = f" · página CNV: {c['pagina_cnv']}" if "pagina_cnv" in c else ""
        L.append(f"- `{c['cuit']}` {c['descripcion']} ({'; '.join(marcas)}){pagina}")
    L += [f"- ⚠ {e}" for e in inf["errores"]]
    return "\n".join(L) + "\n"
