"""`python -m cnv soberanos`: catalogo de los documentos de emision de los titulos publicos nacionales (bonos, letras) desde el
Boletin Oficial, para la biblioteca del asistente y la ficha de cada bono.

Fuente primaria: las resoluciones conjuntas de la Secretaria de Finanzas y la Secretaria de Hacienda (y las resoluciones del
Ministerio de Economia sobre deuda) publicadas en el Boletin Oficial. Cada una dispone la emision o la ampliacion de uno o mas
instrumentos y fija sus condiciones (moneda, vencimiento, amortizacion, interes, ajuste). Se buscan mes a mes con la busqueda
avanzada del Boletin, se lee el texto del aviso y el modelo (Haiku, solo la parte resolutiva) devuelve que instrumentos crea o
amplia. Los instrumentos se vinculan con las especies que cotizan hoy (curvas publicadas en gbr-inversiones-assets) por la
especie que nombra el texto o, si no la nombra, por clase y fecha de vencimiento. Ademas entran fuentes fijas de
data/soberanos_extra.csv (prospecto de los bonos globales en la SEC, por ejemplo).

Incremental: cada aviso se lee una sola vez; el catalogo anterior (--previo) trae lo ya extraido.
Salida: soberanos/catalogo.json (contrato en `construir_catalogo`).
"""
import csv
import datetime as dt
import html as _html
import json
import re
import time

from .concurrencia import crear_mensaje
from .modelo import MODELO

BOLETIN = "https://www.boletinoficial.gob.ar"
ASSETS = "https://raw.githubusercontent.com/gbrinversiones/gbr-inversiones-assets/main"
CURVAS = ("bonos.json", "lecaps.json", "cer.json", "tasa_variable.json", "bopreal.json", "bonos_byma.json", "ars_byma.json")
BUSQUEDAS = (  # (texto, desde, tambien resoluciones del Ministerio de Economia): todas las palabras, primera seccion
    ("Secretaría de Finanzas", "2020-01-01", False),
    ("27.556", "2020-07-01", True),            # reestructuracion de la deuda en ley local (Bonares 2030-2041)
)
DESDE_DEFECTO = "2020-01-01"
MAX_PAGINAS_BUSQUEDA = 10
MAX_CARACTERES_MODELO = 16_000
MAX_TOKENS_SALIDA = 2_000
ESPERA_S = 3.0              # entre pedidos al Boletin: con 1 s cortaba con 429 a los ~250 pedidos
ESPERAS_429_S = (30, 90, 180)
VERSION = 1

_MESES = {m: i + 1 for i, m in enumerate(("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"))}
_MESES["setiembre"] = 9

SISTEMA = (
    "Leés la parte resolutiva de una norma argentina de deuda pública publicada en el Boletín Oficial. Devolvé SOLO un JSON, sin texto "
    "alrededor, con esta forma: {\"instrumentos\": [{\"denominacion\": \"...\", \"especie\": \"S30N6\" o null, \"vencimiento\": \"AAAA-MM-DD\" o null, "
    "\"moneda\": \"ARS\"|\"USD\"|null, \"accion\": \"emision\"|\"ampliacion\"|\"conversion\"|\"canje\"|\"otra\", \"articulo\": \"1\"}]}. "
    "Incluí cada instrumento que la norma emite por primera vez (accion=emision), amplía (ampliacion), entrega en una conversión o canje, "
    "o del que fija condiciones. NO incluyas los títulos que se rescatan o se dan de baja en un canje. \"denominacion\": el nombre tal cual "
    "aparece entre comillas. \"especie\": el código de negociación solo si el texto lo escribe (por ejemplo \"en adelante BONCAP T15E7\" -> "
    "\"T15E7\") o si coincide sin duda con la lista de especies vigentes que se da (misma clase y mismo vencimiento); si no, null. "
    "Nunca inventes datos. Si la norma no emite ni amplía instrumentos, devolvé {\"instrumentos\": []}."
)


# ---------------------------------------------------------------------------
# Boletin Oficial
# ---------------------------------------------------------------------------

def plano(html: str) -> str:
    sin = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    sin = re.sub(r"<br\s*/?>|</p>|</div>|</h\d>", "\n", sin, flags=re.IGNORECASE)
    texto = _html.unescape(re.sub(r"<[^>]+>", " ", sin))
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", texto)).strip()


class Limitado(Exception):
    """El Boletin sigue respondiendo 429 despues de esperar: se corta la corrida y lo que falta queda pendiente."""


def pedir(sesion, metodo: str, url: str, dormir=time.sleep, **kw):
    """GET/POST con espera ante 429 (respeta Retry-After). Lanza Limitado si el limite no se levanta."""
    for espera in (*ESPERAS_429_S, None):
        r = getattr(sesion, metodo)(url, timeout=60, **kw)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        if espera is None:
            raise Limitado(f"429 sostenido en {url[:80]}")
        retry = r.headers.get("Retry-After") or ""
        dormir(int(retry) if retry.isdigit() and int(retry) <= 600 else espera)
    raise Limitado(url)


def parsear_resultados(html: str) -> list:
    """Avisos de una pagina de resultados de la busqueda avanzada: [{id, fecha, rubro, organismo, norma, codigo, url, pdf}]."""
    salida, rubro = [], ""
    for bloque in re.split(r'(?=<h5 class="seccion-rubro|<a href="/detalleAviso/)', html):
        m_rubro = re.match(r'<h5 class="seccion-rubro[^>]*>(.*?)</h5>', bloque, re.DOTALL)
        if m_rubro:
            rubro = plano(m_rubro.group(1)).upper()
            continue
        m = re.match(r'<a href="/detalleAviso/primera/(\d+)/(\d{8})', bloque)
        if not m:
            continue
        items = [plano(x) for x in re.findall(r'<p class="item[^"]*"[^>]*>(.*?)</p>', bloque, re.DOTALL)]
        organismo = items[0] if items else ""
        norma = items[1] if len(items) > 1 else ""
        detalle = items[3] if len(items) > 3 else (items[2] if len(items) > 2 else "")
        f = m.group(2)
        salida.append({
            "id": m.group(1), "fecha": f"{f[:4]}-{f[4:6]}-{f[6:]}", "rubro": rubro, "organismo": organismo, "norma": norma,
            "codigo": detalle.split("\n")[0].strip()[:60],
            "url": f"{BOLETIN}/detalleAviso/primera/{m.group(1)}/{f}", "pdf": f"{BOLETIN}/pdf/aviso/primera/{m.group(1)}/{f}",
        })
    return salida


def es_de_deuda(aviso: dict, ministerio: bool = False) -> bool:
    """Resoluciones conjuntas de Finanzas/Hacienda; con `ministerio`, tambien resoluciones del Ministerio de Economia (solo en busquedas
    acotadas a deuda, como la de la ley 27.556)."""
    org, norma = aviso["organismo"].upper(), aviso["norma"].upper()
    return ("FINANZAS" in org and "CONJUNTA" in norma) or (ministerio and "MINISTERIO DE ECONOM" in org and norma.startswith("RESOLUCI"))


def _params(texto: str, desde: dt.date, hasta: dt.date, pagina: int) -> dict:
    return {
        "busquedaRubro": False, "hayMasResultadosBusqueda": True, "ejecutandoLlamadaAsincronicaBusqueda": False, "ultimaSeccion": "",
        "filtroPorRubrosSeccion": False, "filtroPorRubroBusqueda": False, "filtroPorSeccionBusqueda": False, "busquedaOriginal": True,
        "ordenamientoSegunda": False, "seccionesOriginales": [1], "ultimoItemExterno": None, "ultimoItemInterno": None, "texto": texto,
        "rubros": [], "nroNorma": "", "anioNorma": "", "denominacion": "", "tipoContratacion": "", "anioContratacion": "", "nroContratacion": "",
        "fechaDesde": desde.strftime("%d/%m/%Y"), "fechaHasta": hasta.strftime("%d/%m/%Y"), "todasLasPalabras": True, "comienzaDenominacion": True,
        "seccion": [1], "tipoBusqueda": "Avanzada", "numeroPagina": pagina, "ultimoRubro": "",
    }


def buscar(sesion, texto: str, desde: dt.date, hasta: dt.date) -> list:
    """Avisos de la primera seccion que contienen `texto` entre dos fechas (todas las paginas, hasta MAX_PAGINAS_BUSQUEDA)."""
    vistos, salida, pagina = set(), [], 1
    while pagina and pagina <= MAX_PAGINAS_BUSQUEDA:
        r = pedir(sesion, "post", f"{BOLETIN}/busquedaAvanzada/realizarBusqueda",
                  data={"params": json.dumps(_params(texto, desde, hasta, pagina)), "array_volver": "[]"},
                  headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BOLETIN}/busquedaAvanzada/primera"})
        contenido = r.json().get("content") or {}
        nuevos = [a for a in parsear_resultados(contenido.get("html") or "") if a["id"] not in vistos]
        if not nuevos:
            break
        for a in nuevos:
            vistos.add(a["id"])
            salida.append(a)
        siguiente = str(contenido.get("sig_pag") or "")
        pagina = int(siguiente) if siguiente.isdigit() and int(siguiente) > pagina else 0
        time.sleep(ESPERA_S)
    return salida


def meses(desde: dt.date, hasta: dt.date):
    """[(primer dia, ultimo dia)] de cada mes entre dos fechas."""
    actual = desde.replace(day=1)
    while actual <= hasta:
        proximo = (actual.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        yield max(actual, desde), min(proximo - dt.timedelta(days=1), hasta)
        actual = proximo


def texto_de_aviso(sesion, aviso: dict) -> str:
    """Texto del aviso (desde el organismo hasta el final de los articulos)."""
    t = plano(pedir(sesion, "get", aviso["url"]).text)
    inicio = t.find("Ver texto del aviso")
    t = t[inicio + len("Ver texto del aviso"):] if inicio >= 0 else t
    fin = re.search(r"\n(?:e\. \d{2}/\d{2}/\d{4}|Fecha de publicaci[oó]n \d)", t)
    return (t[:fin.start()] if fin else t).strip()


def parte_resolutiva(texto: str) -> str:
    m = re.search(r"\bRESUELVE[N]?\s*:", texto)
    return (texto[m.start():] if m else texto)[:MAX_CARACTERES_MODELO]


# ---------------------------------------------------------------------------
# Especies vigentes y vinculo
# ---------------------------------------------------------------------------

def clase_de(denominacion: str) -> str:
    """Clase del instrumento por su nombre legal (la que sirve para vincular por vencimiento)."""
    d = (denominacion or "").upper()
    if "DUAL" in d:
        return "dual"
    if "TAMAR" in d:
        return "tamar"
    if re.search(r"\bCER\b", d):
        return "cer"
    if "VINCULAD" in d and "LAR" in d:
        return "dolar_linked"
    if "CAPITALIZABLE" in d:
        return "lecap" if d.lstrip("“\" ").startswith("LETRA") else "boncap"
    if "LAR" in d:      # dolares / dólares
        return "usd"
    return "otra"


def _clase_de_curva(archivo: str, p: dict) -> str:
    t = p.get("ticker", "")
    if archivo == "lecaps.json" or (archivo == "ars_byma.json" and re.fullmatch(r"[ST]\d{2}[A-Z]\d", t)):
        return "lecap" if t.startswith("S") else "boncap"
    if archivo == "cer.json":
        return "cer"
    if archivo == "tasa_variable.json":
        c = (p.get("categoria") or "").lower()
        return "dual" if "dual" in c else ("dolar_linked" if "lar" in c else "tamar")
    if archivo in ("bonos.json", "bopreal.json"):
        return "usd"
    if archivo == "bonos_byma.json" and re.fullmatch(r"D\d{2}[A-Z]\d|TZV[A-Z0-9]+", t):
        return "dolar_linked"
    return ""


def vigentes(traer_json) -> dict:
    """{TICKER: {"clase","vencimiento","nombre","curva"}} de las curvas publicadas (solo titulos nacionales, sin provincias ni ONs)."""
    salida = {}
    for archivo in CURVAS:
        try:
            datos = traer_json(f"{ASSETS}/{archivo}")
        except Exception as error:  # noqa: BLE001  una curva caida no frena el resto
            print(f"  curva {archivo}: {str(error)[:100]}", flush=True)
            continue
        puntos = datos.get("puntos") if isinstance(datos, dict) else datos
        for p in puntos or []:
            t = str(p.get("ticker") or "").upper()
            clase = _clase_de_curva(archivo, p)
            if not t or not clase or t in salida:
                continue
            salida[t] = {"clase": clase, "vencimiento": str(p.get("fecha_vencimiento") or p.get("vencimiento") or "")[:10],
                         "nombre": p.get("nombre") or "", "curva": archivo}
    return salida


def vincular(instrumento: dict, especies: dict) -> str | None:
    """Especie vigente del instrumento: la que nombra el texto si existe y su vencimiento coincide; si no, la unica de la misma clase
    con el mismo vencimiento. None si no hay una sola."""
    venc = instrumento.get("vencimiento") or ""
    e = str(instrumento.get("especie") or "").upper()
    if e in especies and (not venc or not especies[e]["vencimiento"] or especies[e]["vencimiento"] == venc):
        return e
    if not venc:
        return None
    clase = clase_de(instrumento.get("denominacion", ""))
    candidatas = [t for t, x in especies.items() if x["vencimiento"] == venc and x["clase"] == clase]
    return candidatas[0] if len(candidatas) == 1 else None


# ---------------------------------------------------------------------------
# Modelo
# ---------------------------------------------------------------------------

def _lista_especies(especies: dict) -> str:
    return "\n".join(f"{t}: {x['clase']}, vence {x['vencimiento'] or '?'}" for t, x in sorted(especies.items()))


def interpretar(salida_modelo: str) -> list:
    m = re.search(r"\{.*\}", salida_modelo or "", re.DOTALL)
    if not m:
        return []
    try:
        datos = json.loads(m.group(0))
    except ValueError:
        return []
    instrumentos = []
    for x in datos.get("instrumentos") or []:
        if not isinstance(x, dict) or not str(x.get("denominacion") or "").strip():
            continue
        venc = str(x.get("vencimiento") or "")
        instrumentos.append({
            "denominacion": str(x["denominacion"]).strip()[:250],
            "especie": (str(x.get("especie")).upper().strip() if x.get("especie") else None),
            "vencimiento": venc if re.fullmatch(r"\d{4}-\d{2}-\d{2}", venc) else None,
            "moneda": x.get("moneda") if x.get("moneda") in ("ARS", "USD") else None,
            "accion": x.get("accion") if x.get("accion") in ("emision", "ampliacion", "conversion", "canje", "otra") else "otra",
            "articulo": str(x.get("articulo") or "")[:10],
        })
    return instrumentos


def extraer(cliente, texto: str, especies: dict) -> list:
    r = crear_mensaje(
        cliente, model=MODELO, max_tokens=MAX_TOKENS_SALIDA, thinking={"type": "disabled"}, system=SISTEMA,
        messages=[{"role": "user", "content": f"Especies vigentes (código: clase, vencimiento):\n{_lista_especies(especies)}\n\nNorma:\n{parte_resolutiva(texto)}"}],
    )
    return interpretar("".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", None) == "text"))


# ---------------------------------------------------------------------------
# Catalogo
# ---------------------------------------------------------------------------

def leer_extras(ruta: str = "data/soberanos_extra.csv") -> list:
    try:
        with open(ruta, encoding="utf-8") as f:
            return [{k: (v or "").strip() for k, v in fila.items()} for fila in csv.DictReader(f) if (fila.get("especies") or "").strip()]
    except FileNotFoundError:
        return []


def construir_catalogo(normas: dict, especies: dict, extras: list, ahora: dt.datetime) -> dict:
    """{"version","generado","titulos":{ESPECIE:{especie,denominacion,clase,vencimiento,documentos:[...]}},"normas":{id:{...}}}.
    Documento: {"id","accion","norma","organismo","fecha","url","pdf","fuente"} (fuente "boletin_oficial" o la del extra). Por especie, la
    emision original primero y despues el resto de la mas nueva a la mas vieja."""
    titulos = {}

    def agregar(especie, denominacion, doc):
        t = titulos.setdefault(especie, {"especie": especie, "denominacion": denominacion, "clase": especies.get(especie, {}).get("clase", ""),
                                         "vencimiento": especies.get(especie, {}).get("vencimiento", ""), "documentos": []})
        if not t["denominacion"] and denominacion:
            t["denominacion"] = denominacion
        if all(d["id"] != doc["id"] or d["accion"] != doc["accion"] for d in t["documentos"]):
            t["documentos"].append(doc)

    for aviso_id, n in normas.items():
        for ins in n.get("instrumentos") or []:
            especie = vincular(ins, especies)
            ins["vinculo"] = especie
            if especie:
                agregar(especie, ins["denominacion"], {"id": f"bo-{aviso_id}", "accion": ins["accion"], "norma": n["norma"], "organismo": n["organismo"],
                                                      "fecha": n["fecha"], "url": n["url"], "pdf": n["pdf"], "fuente": "boletin_oficial"})
    for x in extras:
        for especie in [e.strip().upper() for e in x["especies"].split("|") if e.strip()]:
            agregar(especie, x.get("denominacion", ""), {"id": x["id"], "accion": x.get("accion") or "prospecto", "norma": x["titulo"],
                                                          "organismo": x["organismo"], "fecha": x["fecha"], "url": x["url"], "pdf": x.get("pdf") or "",
                                                          "fuente": x.get("fuente") or "otra"})
    orden = {"emision": 0, "prospecto": 0}
    for t in titulos.values():
        t["documentos"].sort(key=lambda d: (orden.get(d["accion"], 1), "" if orden.get(d["accion"], 1) == 0 else _inv(d["fecha"])))
    return {"version": VERSION, "generado": ahora.isoformat(timespec="seconds"), "titulos": dict(sorted(titulos.items())), "normas": normas}


def _inv(fecha: str) -> str:
    return "".join(chr(ord("9") - ord(c) + ord("0")) if c.isdigit() else c for c in fecha)


def ejecutar(sesion, cliente, previo: dict, especies: dict, hasta: dt.date, hasta_t: float, max_normas: int, busquedas=BUSQUEDAS) -> tuple:
    """(normas, buscado_hasta, pendientes, informe). Busca mes a mes desde el ultimo mes ya mirado (el mes en curso siempre se vuelve a
    mirar), lee los avisos nuevos y los manda al modelo. Lo que no entra en esta corrida queda en `pendientes` para la proxima."""
    from .concurrencia import ErrorDeCuenta

    normas = dict(previo.get("normas") or {})
    buscado = dict(previo.get("buscado_hasta") or {})
    candidatos = {a["id"]: a for a in previo.get("pendientes") or []}
    informe = {"avisos_nuevos": 0, "leidas": 0, "sin_instrumentos": 0, "fallidas": 0, "pendientes": 0, "descartadas": 0}
    corte = None
    for texto, desde_txt, ministerio in busquedas:
        if corte:
            break
        desde = dt.date.fromisoformat(max(desde_txt, buscado.get(texto, "")))
        for d0, d1 in meses(desde, hasta):
            try:
                for a in buscar(sesion, texto, d0, d1):
                    if es_de_deuda(a, ministerio) and a["id"] not in normas:
                        candidatos.setdefault(a["id"], a)
            except Exception as error:  # noqa: BLE001  un mes que no responde se vuelve a buscar en la proxima corrida
                print(f"  busqueda '{texto}' {d0:%Y-%m}: {str(error)[:100]}", flush=True)
                buscado[texto] = d0.replace(day=1).isoformat()
                if isinstance(error, Limitado):
                    corte = str(error)
                break
            buscado[texto] = d0.replace(day=1).isoformat()
            time.sleep(ESPERA_S)
    informe["avisos_nuevos"] = len(candidatos)
    pendientes = []
    # de la mas nueva a la mas vieja: las especies que cotizan hoy salen de las normas recientes
    for a in sorted(candidatos.values(), key=lambda x: (x["fecha"], int(x["id"])), reverse=True):
        if corte or informe["leidas"] + informe["fallidas"] >= max_normas or time.monotonic() > hasta_t:
            pendientes.append(a)
            continue
        try:
            texto = texto_de_aviso(sesion, a)
            if "CONJUNTA" not in a["norma"].upper() and not re.search(r"\b(t[ií]tulos?|bonos?|letras?)\b", texto, re.IGNORECASE):
                informe["descartadas"] += 1
                normas[a["id"]] = {**a, "instrumentos": []}
                continue
            instrumentos = extraer(cliente, texto, especies)
        except (ErrorDeCuenta, Limitado) as error:     # tope de dolares, limite de la cuenta o del Boletin: lo que falta queda para la proxima
            corte = str(error)
            pendientes.append(a)
            continue
        except Exception as error:  # noqa: BLE001  se reintenta en la proxima corrida
            informe["fallidas"] += 1
            pendientes.append(a)
            print(f"  {a['norma']} ({a['fecha']}): {str(error)[:120]}", flush=True)
            continue
        normas[a["id"]] = {**a, "instrumentos": instrumentos}
        informe["leidas"] += 1
        informe["sin_instrumentos"] += not instrumentos
        print(f"  {a['norma']} ({a['fecha']}): {len(instrumentos)} instrumentos "
              + ", ".join(sorted({i['especie'] or i['vencimiento'] or '?' for i in instrumentos}))[:200], flush=True)
        time.sleep(ESPERA_S)
    informe["pendientes"] = len(pendientes)
    if corte:
        informe["corte"] = corte[:160]
    return normas, buscado, pendientes, informe
