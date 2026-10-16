"""Cruce con renta fija: que tan cubierto esta cada emisor de ONs (y cada empresa del universo) con documentos y datos verificados.

Une tres cosas, sin modelo y sin red: data/universo.csv (empresas con CUIT), data/ons.csv (cada ON con el CUIT de su emisor) y lo que ya
hay en el almacen (indices por empresa, cifras de estados contables y avisos de pago estructurados).

Salida: cnv/indice/cobertura.json.gz
  {"version": 1, "al": "AAAA-MM-DD", "resumen": {...}, "emisores": [...], "ons_sin_emisor": [...]}
  emisor = {cuit, empresa, ons: [{ticker, vence, ultimo_pago}], documentos: {tipo: {total, con_texto, ultimo}},
            ultimo_estado_contable, ultimo_aviso_pago, huecos: [codigo, ...]}
Los "huecos" son avisos accionables (falta algo, algo fallo o esta viejo); no son errores del programa.
"""
import csv
import datetime as dt
import json

from . import almacen as alm
from .avisos_pago import leer_avisos
from .financieros import leer_financieros
from .sincronizar import TIPOS_TEXTO

DIAS_EC_VIEJO = 200  # un estado contable cuyo cierre es anterior a esto esta desactualizado (el ultimo trimestre ya deberia estar presentado)
DIAS_SIN_DOCUMENTOS = 120
VERSION = 1

HUECOS = {
    "sin_documentos": "no hay ningun documento de la empresa en el indice",
    "sin_estados_contables": "no hay estados contables con texto",
    "estados_contables_sin_cifras": "hay estados contables con texto pero ninguno dio cifras verificadas",
    "estado_contable_desactualizado": f"el cierre del ultimo estado contable con cifras es de hace mas de {DIAS_EC_VIEJO} dias",
    "sin_prospectos": "emisor de ONs sin prospectos/suplementos/avisos de emision con texto",
    "sin_avisos_de_pago": "emisor de ONs sin avisos de pago con texto",
    "avisos_de_pago_sin_estructurar": "hay avisos de pago con texto pero ninguno quedo estructurado",
    "documentos_fallidos": "hay documentos que no se pudieron bajar (ver cnv salud)",
    "documentos_sin_bajar": "hay documentos listados que todavia no se bajaron",
    "sin_actividad_reciente": f"sin documentos nuevos hace mas de {DIAS_SIN_DOCUMENTOS} dias",
}


def leer_ons(ruta: str = "data/ons.csv") -> list:
    """[{ticker, emisor, descripcion, vence, cuit}] de data/ons.csv. El CUIT puede estar vacio (ON sin emisor resuelto)."""
    with open(ruta, encoding="utf-8", newline="") as f:
        return [{"ticker": (r.get("ticker") or "").strip(), "emisor": (r.get("emisor") or "").strip(), "descripcion": (r.get("descripcion") or "").strip(),
                 "vence": (r.get("vence") or "").strip() or None, "cuit": (r.get("cuit") or "").replace("-", "").strip()} for r in csv.DictReader(f) if (r.get("ticker") or "").strip()]


def _fecha(s):
    try:
        return dt.date.fromisoformat(s) if isinstance(s, str) and s else None
    except ValueError:
        return None


def _ultimo_estado_contable(financieros: dict):
    periodos = [p for p in financieros["periodos"] if p.get("periodo")]
    if not periodos:
        return None
    p = max(periodos, key=lambda p: (p["periodo"], int(p["doc"])))
    return {"doc": p["doc"], "periodo": p["periodo"], "tipo": p.get("tipo"), "balance": p.get("balance", "desconocido"),
            "fecha_presentacion": p.get("fecha_presentacion"), "cifras": len(p.get("cifras") or {})}


def _ultimo_aviso_pago(avisos: dict, indice: list):
    pagos = avisos["pagos"]
    if pagos:
        p = max(pagos, key=lambda p: (p.get("fecha_presentacion") or "", int(p["doc"]), (p.get("fecha_pago") or {}).get("valor") or ""))
        v = lambda c: (p.get(c) or {}).get("valor")  # noqa: E731
        return {"doc": p["doc"], "fecha_presentacion": p.get("fecha_presentacion"), "fecha_pago": v("fecha_pago"), "tipo_pago": v("tipo_pago"),
                "clase": v("clase"), "ticker": v("ticker"), "estructurado": True}
    docs = [m for m in indice if m.get("t") == "avisos_pago" and m.get("texto_ok")]
    if not docs:
        return None
    m = max(docs, key=lambda m: (m.get("f") or "", int(m["id"])))
    return {"doc": m["id"], "fecha_presentacion": m.get("f"), "fecha_pago": None, "tipo_pago": None, "clase": None, "ticker": None, "estructurado": False}


def _emisor(almacen, e: dict, ons: list, hoy: dt.date) -> dict:
    indice = alm.leer_indice_empresa(almacen, e["cuit"])
    financieros, avisos = leer_financieros(almacen, e["cuit"]), leer_avisos(almacen, e["cuit"])
    docs = {}
    for m in indice:
        d = docs.setdefault(m.get("t") or "otros", {"total": 0, "con_texto": 0, "ultimo": None})
        d["total"] += 1
        d["con_texto"] += bool(m.get("texto_ok"))
        f = m.get("f")
        if f and (d["ultimo"] is None or f > d["ultimo"]):
            d["ultimo"] = f
    ultimo_ec, ultimo_pago = _ultimo_estado_contable(financieros), _ultimo_aviso_pago(avisos, indice)
    pagos_por_ticker = {}
    for p in avisos["pagos"]:
        t = (p.get("ticker") or {}).get("valor")
        fp = (p.get("fecha_pago") or {}).get("valor")
        if t and fp and fp > pagos_por_ticker.get(t, ""):
            pagos_por_ticker[t] = fp
    mis_ons = [{"ticker": o["ticker"], "vence": o["vence"], "ultimo_pago": pagos_por_ticker.get(o["ticker"])} for o in ons]

    def con_texto(t):
        return docs.get(t, {}).get("con_texto", 0)

    huecos = []
    if not indice:
        huecos.append("sin_documentos")
    if con_texto("estados_contables") == 0:
        huecos.append("sin_estados_contables")
    elif ultimo_ec is None:
        huecos.append("estados_contables_sin_cifras")
    if ultimo_ec and (hoy - (_fecha(ultimo_ec["periodo"]) or hoy)).days > DIAS_EC_VIEJO:
        huecos.append("estado_contable_desactualizado")
    if mis_ons:
        if con_texto("prospectos") == 0:
            huecos.append("sin_prospectos")
        if con_texto("avisos_pago") == 0:
            huecos.append("sin_avisos_de_pago")
        elif not avisos["pagos"]:
            huecos.append("avisos_de_pago_sin_estructurar")
    if any(not m.get("texto_ok") and m.get("t") in TIPOS_TEXTO and m.get("intentos", 0) > 0 for m in indice):
        huecos.append("documentos_fallidos")
    if any(not m.get("texto_ok") and m.get("t") in TIPOS_TEXTO and not m.get("intentos", 0) for m in indice):
        huecos.append("documentos_sin_bajar")
    ultima = max((_fecha(m.get("f")) for m in indice if _fecha(m.get("f"))), default=None)
    if ultima and (hoy - ultima).days > DIAS_SIN_DOCUMENTOS:
        huecos.append("sin_actividad_reciente")
    return {"cuit": e["cuit"], "empresa": e["empresa"], "ons": mis_ons, "documentos": {t: docs[t] for t in sorted(docs)},
            "ultimo_estado_contable": ultimo_ec, "ultimo_aviso_pago": ultimo_pago, "huecos": huecos}


def calcular(almacen, universo: list, ons: list, hoy: dt.date | None = None) -> dict:
    """Informe completo (dict serializable). `universo`: [{empresa, cuit}]; `ons`: filas de data/ons.csv."""
    hoy = hoy or dt.date.today()
    por_cuit = {}
    for o in ons:
        if o["cuit"]:
            por_cuit.setdefault(o["cuit"], []).append(o)
    cuits = {e["cuit"] for e in universo}
    emisores = [_emisor(almacen, e, sorted(por_cuit.get(e["cuit"], []), key=lambda o: o["ticker"]), hoy) for e in sorted(universo, key=lambda e: e["empresa"])]
    sin_emisor = []
    for o in ons:
        if not o["cuit"]:
            sin_emisor.append({"ticker": o["ticker"], "emisor": o["emisor"], "motivo": "sin_cuit_en_ons_csv"})
        elif o["cuit"] not in cuits:
            sin_emisor.append({"ticker": o["ticker"], "emisor": o["emisor"], "cuit": o["cuit"], "motivo": "emisor_fuera_del_universo"})
    cuenta = {}
    for e in emisores:
        for h in e["huecos"]:
            cuenta[h] = cuenta.get(h, 0) + 1
    con_ons = [e for e in emisores if e["ons"]]
    resumen = {
        "empresas": len(emisores), "emisores_con_ons": len(con_ons), "ons": len(ons), "ons_vinculadas": sum(len(e["ons"]) for e in emisores), "ons_sin_emisor": len(sin_emisor),
        "con_estado_contable": sum(1 for e in emisores if e["ultimo_estado_contable"]), "con_aviso_de_pago": sum(1 for e in emisores if e["ultimo_aviso_pago"]),
        "emisores_con_ons_y_estado_contable": sum(1 for e in con_ons if e["ultimo_estado_contable"]),
        "emisores_con_ons_y_aviso_de_pago": sum(1 for e in con_ons if e["ultimo_aviso_pago"]),
        "huecos": dict(sorted(cuenta.items())),
    }
    return {"version": VERSION, "al": hoy.isoformat(), "huecos_definicion": HUECOS, "resumen": resumen, "emisores": emisores, "ons_sin_emisor": sin_emisor}


def construir(almacen, universo: list, ons: list, hoy: dt.date | None = None) -> dict:
    """Escribe cnv/indice/cobertura.json.gz (sin reescribirlo si no cambio) y devuelve el resumen."""
    informe = calcular(almacen, universo, ons, hoy)
    alm.guardar_si_cambio(almacen, "cnv/indice/cobertura.json.gz", alm.comprimir(json.dumps(informe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
    return informe["resumen"]
