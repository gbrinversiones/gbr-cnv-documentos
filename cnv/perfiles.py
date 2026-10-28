"""Perfil de emisor DETERMINISTA: un texto plantilla en español simple por emisor, armado SOLO con datos ya verificados. No usa el modelo ni la red.

Cada frase sale de una plantilla fija (sin adjetivos valorativos: no dice si algo es «bueno», «alto» o «preocupante», solo que figura) y lleva el
`doc` de la CNV de donde viene (`docs`) o, si no hay documento (el universo, el catalogo de ONs, los huecos), la `fuente`. Si un dato falta, la frase
no se escribe: no se rellena ni se estima.

Fuentes: ratios y cifras de cnv/financieros (via cnv/ratios.py: la fila de la ficha, consolidado primero), condiciones de emision verificadas de las ON
(cnv/indice/condiciones.json.gz), indice de documentos por empresa (hechos relevantes con su categoria) y huecos de cnv/cobertura.py.

Salida: cnv/indice/perfiles.json.gz
  {"version": 1, "al": "AAAA-MM-DD", "perfiles": {cuit: {"empresa", "texto", "frases": [{"tipo", "texto", "docs": [doc_id, ...], "fuente"}]}}}
"""
import datetime as dt
import gzip
import json

from . import almacen as alm
from . import cobertura as cob
from .auditoria import leer_sospechosas
from .financieros import leer_financieros, moneda_canonica
from .ratios import periodos_vigentes, ratios_de_periodo, seleccionar_ficha

VERSION = 1
N_HECHOS = 3

# ratio -> (nombre, como se escribe el valor, como se calcula)
RATIOS = {
    "liquidez_corriente": ("Liquidez corriente", "veces", "activo corriente dividido pasivo corriente"),
    "endeudamiento": ("Endeudamiento", "veces", "pasivo total dividido patrimonio neto"),
    "deuda_ebitda": ("Deuda financiera sobre EBITDA", "veces", "deuda financiera dividida resultado operativo mas depreciaciones y amortizaciones"),
    "margen_neto": ("Margen neto", "porcentaje", "resultado neto dividido ventas"),
    "roe": ("Rentabilidad sobre patrimonio", "porcentaje", "resultado neto dividido patrimonio neto"),
    "cobertura_intereses": ("Cobertura de intereses", "veces", "resultado operativo dividido intereses"),
    "deuda_neta_ebitda": ("Deuda neta sobre EBITDA", "veces", "deuda financiera menos caja, dividida EBITDA"),
    "cobertura_deuda_cp": ("Caja sobre deuda financiera corriente", "veces", "caja dividida deuda financiera corriente"),
    "fcf_deuda": ("Flujo operativo menos inversiones sobre deuda financiera", "porcentaje", "flujo de efectivo operativo menos inversiones, dividido deuda financiera"),
}
MOTIVOS = {
    "falta_cifra": "falta una cifra en el estado contable", "periodo_no_anual": "solo se calcula con períodos anuales", "cifra_sospechosa": "una cifra no pasó la auditoría contable",
    "patrimonio_negativo": "el patrimonio neto es negativo", "ebitda_negativo": "el EBITDA es negativo", "division_por_cero": "el denominador es cero",
    "sin_linea_intereses": "el estado contable no trae una línea de intereses", "periodos_distintos": "las cifras son de períodos distintos",
}
CIFRAS = (("ventas", "Ventas"), ("resultado_neto", "Resultado neto"), ("activo_total", "Activo total"), ("pasivo_total", "Pasivo total"), ("patrimonio_neto", "Patrimonio neto"))
CATEGORIAS = {
    "resultados": "resultados", "dividendos": "dividendos", "recompra_acciones": "recompra de acciones", "recompra_on": "recompra de obligaciones negociables",
    "emision_on": "emisión de obligaciones negociables", "cambio_directorio": "cambio de directorio", "adquisicion_venta": "adquisición o venta",
    "hecho_judicial": "hecho judicial", "convocatoria_asamblea": "convocatoria a asamblea", "calificacion_riesgo": "calificación de riesgo", "otro": "otro tema",
}
TIPO_PERIODO = {"anual": "anual", "trimestral": "trimestral", "semestral": "semestral"}
BALANCE = {"consolidado": "consolidado", "individual": "individual", "desconocido": "de tipo no informado"}
MONEDA = {"pesos": "en pesos", "dolares": "en dólares"}


def num(x: float, decimales: int = 2) -> str:
    """Formato argentino: punto de miles y coma decimal (1.234.567,89)."""
    t = f"{x:,.{decimales}f}"
    return t.replace(",", "X").replace(".", ",").replace("X", ".")


def fecha(iso) -> str:
    try:
        return dt.date.fromisoformat(iso).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return str(iso)


def _frase(tipo: str, texto: str, docs=None, fuente: str = "") -> dict:
    return {"tipo": tipo, "texto": texto, "docs": sorted({str(d) for d in docs or []}, key=int), "fuente": fuente}


def _valor_ratio(nombre: str, v: float) -> str:
    return f"{num(v * 100, 1)} %" if RATIOS[nombre][1] == "porcentaje" else f"{num(v)} veces"


def frases_estado_contable(f: dict, periodo_fuente: dict | None) -> list:
    """Frases del ultimo cierre (fila de la ficha): periodo y tipo de balance, cifras principales y ratios."""
    doc = f["doc"]
    moneda = MONEDA.get(moneda_canonica(f.get("moneda")), f"en {f.get('moneda')}" if f.get("moneda") else "en moneda no informada")
    balance = BALANCE.get(f.get("balance"), "de tipo no informado")
    salida = [_frase("estado_contable", f"Su último estado contable con cifras verificadas es del período {TIPO_PERIODO.get(f.get('tipo'), f.get('tipo') or '')} cerrado el {fecha(f['periodo'])}, "
                                         f"balance {balance}, {moneda} (documento {doc} de la CNV)." + (" Las cifras fueron leídas por OCR de un PDF escaneado (el balance cuadra y cada cifra tiene cita), por lo que pueden tener errores de lectura." if (periodo_fuente or {}).get("ocr") else ""), [doc])]
    cifras = (periodo_fuente or {}).get("cifras") or {}
    unidad = f.get("unidad") or "unidad no informada"
    partes = [f"{nombre.lower()} {num(cifras[campo]['valor'], 0 if float(cifras[campo]['valor']).is_integer() else 2)}" for campo, nombre in CIFRAS if campo in cifras and isinstance(cifras[campo].get("valor"), (int, float))]
    if partes:
        salida.append(_frase("cifras", f"Cifras tal como figuran en el documento, expresadas en {unidad}: " + ", ".join(partes) + ".", [doc]))
    calculados = [(n, v) for n, v in f["ratios"].items() if v is not None and n in RATIOS]
    for nombre, v in calculados:
        etiqueta, _, formula = RATIOS[nombre]
        salida.append(_frase("ratio", f"{etiqueta}: {_valor_ratio(nombre, v)} ({formula}).", [doc]))
    sin = [(n, m) for n, m in f["motivos"].items() if n in RATIOS]
    if sin:
        detalle = "; ".join(f"{RATIOS[n][0].lower()} ({MOTIVOS.get(m.split(':')[0], 'no se pudo calcular')})" for n, m in sorted(sin))
        salida.append(_frase("ratios_sin_calcular", f"No se pudieron calcular: {detalle}.", [doc]))
    return salida


def frases_ons(ons: list, condiciones: dict) -> list:
    """Una frase con las ON vinculadas del catalogo, y una por cada ON con vencimiento / tasa verificados en el documento de emision."""
    if not ons:
        return []
    lista = ", ".join(f"{o['ticker']}" + (f" (vence {fecha(o['vence'])})" if o.get("vence") else "") for o in sorted(ons, key=lambda o: o["ticker"]))
    salida = [_frase("ons", f"En el catálogo de obligaciones negociables figuran {len(ons)}: {lista}.", fuente="data/ons.csv")]
    for o in sorted(ons, key=lambda o: o["ticker"]):
        c = condiciones.get(o["ticker"])
        if not c:
            continue
        campos, fuentes = c["campos"], c["fuentes"]
        datos, docs = [], []
        if "fecha_vencimiento" in campos:
            datos.append(f"vence el {fecha(campos['fecha_vencimiento'])}")
            docs.append(fuentes["fecha_vencimiento"]["doc"])
        if "tasa_tipo" in campos:
            tasa = f"tasa {campos['tasa_tipo']}"
            docs.append(fuentes["tasa_tipo"]["doc"])
            if campos["tasa_tipo"] == "fija" and "tasa_valor" in campos:
                tasa += f" de {num(campos['tasa_valor'])} %"
                docs.append(fuentes["tasa_valor"]["doc"])
            datos.append(tasa)
        if datos:
            salida.append(_frase("on_condiciones", f"Según su documento de emisión, la obligación negociable {o['ticker']} " + ", ".join(datos) + ".", docs))
    return salida


def frases_hechos(indice: list, n: int = N_HECHOS) -> list:
    hechos = sorted((m for m in indice if m.get("t") == "hechos"), key=lambda m: (m.get("f") or "", int(m["id"])), reverse=True)[:n]
    salida = []
    for m in hechos:
        cat = CATEGORIAS.get(m.get("k"), "sin categoría asignada") if m.get("k") else "sin categoría asignada"
        salida.append(_frase("hecho_relevante", f"El {fecha(m.get('f'))} presentó un hecho relevante (categoría: {cat}).", [m["id"]]))
    return salida


def frases_huecos(huecos: list) -> list:
    if not huecos:
        return []
    return [_frase("huecos", "Información que falta o está desactualizada en la base: " + "; ".join(cob.HUECOS[h] for h in huecos if h in cob.HUECOS) + ".", fuente="cnv/indice/cobertura.json.gz")]


def perfil(e: dict, ficha: list, periodos: list, ons: list, condiciones: dict, indice: list, huecos: list) -> dict:
    frases = [_frase("empresa", f"{e['empresa']}, CUIT {e['cuit']}.", fuente="data/universo.csv")]
    if ficha:
        ultimo = ficha[0]
        fuente = next((p for p in periodos if p["doc"] == ultimo["doc"] and moneda_canonica(p.get("moneda")) == moneda_canonica(ultimo.get("moneda"))), None)
        frases += frases_estado_contable(ultimo, fuente)
    else:
        frases.append(_frase("estado_contable", "No hay estados contables con cifras verificadas en la base.", fuente="cnv/financieros"))
    frases += frases_ons(ons, condiciones)
    frases += frases_hechos(indice)
    frases += frases_huecos(huecos)
    return {"empresa": e["empresa"], "texto": " ".join(f["texto"] for f in frases), "frases": frases}


def _leer_gz(almacen, nombre):
    crudo = almacen.leer(nombre)
    if not crudo:
        return None
    try:
        return json.loads(gzip.decompress(crudo))
    except (OSError, ValueError):
        return None


def construir(almacen, universo: list, ons: list, hoy: dt.date | None = None) -> dict:
    """Escribe cnv/indice/perfiles.json.gz (sin reescribirlo si no cambio). Devuelve {"perfiles", "frases", "sin_estado_contable"}."""
    hoy = hoy or dt.date.today()
    informe = cob.calcular(almacen, universo, ons, hoy)
    huecos = {e["cuit"]: e["huecos"] for e in informe["emisores"]}
    por_cuit = {}
    for o in ons:
        if o.get("cuit"):
            por_cuit.setdefault(o["cuit"], []).append(o)
    condiciones = (_leer_gz(almacen, "cnv/indice/condiciones.json.gz") or {}).get("ons", {})
    sospechosas = leer_sospechosas(almacen)
    perfiles, n_frases, sin_ec = {}, 0, 0
    for e in sorted(universo, key=lambda e: (e["empresa"], e["cuit"])):
        periodos = periodos_vigentes(leer_financieros(almacen, e["cuit"])["periodos"])
        filas = [ratios_de_periodo(p, sospechosas.get(e["cuit"], {}).get(p["doc"], frozenset())) for p in periodos]
        ficha = seleccionar_ficha(filas)
        mis_ons = por_cuit.get(e["cuit"], [])
        perfiles[e["cuit"]] = perfil(e, ficha, periodos, mis_ons, {o["ticker"]: condiciones[o["ticker"]] for o in mis_ons if o["ticker"] in condiciones},
                                     alm.leer_indice_empresa(almacen, e["cuit"]), huecos.get(e["cuit"], []))
        n_frases += len(perfiles[e["cuit"]]["frases"])
        sin_ec += not ficha
    salida = {"version": VERSION, "al": hoy.isoformat(), "perfiles": perfiles}
    alm.guardar_si_cambio(almacen, "cnv/indice/perfiles.json.gz", alm.comprimir(json.dumps(salida, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
    return {"perfiles": len(perfiles), "frases": n_frases, "sin_estado_contable": sin_ec}
