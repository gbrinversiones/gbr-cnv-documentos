"""Alertas de novedades: documentos nuevos entre dos corridas y lo presentado en los ultimos N dias (7 por defecto).

Dos archivos, ambos nuevos:
  cnv/indice/vistos.json      {"version": 1, "cuits": {cuit: {doc_id: "AAAA-MM-DD"}}}: la fecha en que se VIO cada hecho relevante / aviso de pago
                              por primera vez. Es el estado entre corridas. Si no existe (primera corrida) se siembra con la fecha de presentacion de cada
                              documento: lo que ya estaba no cuenta como "nuevo" ni inunda la lista.
  cnv/indice/novedades.json   lo que muestra la landing:
      {"version": 1, "al", "desde", "dias", "resumen": {...},
       "hechos_por_categoria": {categoria: [{cuit, empresa, doc, fecha, titulo, resumen, url, categoria, detectado, texto_ok}]},
       "avisos_pago": [{cuit, empresa, doc, fecha_presentacion, estructurado, fecha_pago, tipo_pago, clase, ticker, monto_total, moneda, url, detectado}]}
Una novedad es un documento PRESENTADO en la ventana (fecha de presentacion >= al - dias). `detectado` es cuando el sistema lo vio: un documento
presentado hace 3 dias y detectado hoy es una novedad "de hoy"; una carga historica no llena la lista porque sus fechas de presentacion son viejas.
Sin modelo y sin red.
"""
import datetime as dt
import json

from . import almacen as alm
from .avisos_pago import leer_avisos
from .clasificacion import CATEGORIAS

VERSION = 1
DIAS = 7
TIPOS_VIGILADOS = ("hechos", "avisos_pago")
SIN_CLASIFICAR = "sin_clasificar"
RUTA_VISTOS = "cnv/indice/vistos.json"
RUTA_NOVEDADES = "cnv/indice/novedades.json"


def _json(datos) -> bytes:
    return json.dumps(datos, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def leer_vistos(almacen) -> dict | None:
    """{cuit: {doc_id: fecha}} o None si todavia no existe (primera corrida) o esta ilegible (se vuelve a sembrar)."""
    crudo = almacen.leer(RUTA_VISTOS)
    if not crudo:
        return None
    try:
        d = json.loads(crudo)
        return d["cuits"] if d.get("version") == VERSION and isinstance(d["cuits"], dict) else None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def actualizar_vistos(almacen, empresas: list, hoy: dt.date) -> tuple:
    """(vistos, nuevos): marca los documentos vigilados que no estaban; `nuevos` es {(cuit, doc_id)} de los detectados en ESTA corrida.
    En la primera corrida se siembra con la fecha de presentacion y no hay nuevos."""
    previos = leer_vistos(almacen)
    sembrando = previos is None
    vistos = {c: dict(d) for c, d in (previos or {}).items()}
    nuevos = set()
    for e in empresas:
        propios = vistos.setdefault(e["cuit"], {})
        for m in alm.leer_indice_empresa(almacen, e["cuit"]):
            if m.get("t") not in TIPOS_VIGILADOS or m["id"] in propios:
                continue
            if sembrando:
                propios[m["id"]] = m.get("f") or hoy.isoformat()
            else:
                propios[m["id"]] = hoy.isoformat()
                nuevos.add((e["cuit"], m["id"]))
    vistos = {c: d for c, d in vistos.items() if d}
    alm.guardar_si_cambio(almacen, RUTA_VISTOS, _json({"version": VERSION, "cuits": vistos}))
    return vistos, nuevos


def _fecha(s):
    try:
        return dt.date.fromisoformat(s) if isinstance(s, str) and s else None
    except ValueError:
        return None


def calcular(almacen, empresas: list, hoy: dt.date | None = None, dias: int = DIAS) -> dict:
    """Actualiza `vistos` y arma el informe de novedades (dict serializable)."""
    hoy = hoy or dt.date.today()
    desde = hoy - dt.timedelta(days=dias)
    vistos, nuevos = actualizar_vistos(almacen, empresas, hoy)
    por_categoria, avisos = {}, []
    for e in empresas:
        indice = alm.leer_indice_empresa(almacen, e["cuit"])
        propios = vistos.get(e["cuit"], {})
        pagos_por_doc = {}
        for p in leer_avisos(almacen, e["cuit"])["pagos"]:
            pagos_por_doc.setdefault(p["doc"], []).append(p)
        for m in indice:
            f = _fecha(m.get("f"))
            if m.get("t") not in TIPOS_VIGILADOS or f is None or f < desde or f > hoy:
                continue
            detectado = propios.get(m["id"], hoy.isoformat())
            if m["t"] == "hechos":
                k = m.get("k") if m.get("k") in CATEGORIAS else SIN_CLASIFICAR
                por_categoria.setdefault(k, []).append({
                    "cuit": e["cuit"], "empresa": e["empresa"], "doc": m["id"], "fecha": m["f"], "titulo": m.get("d"), "resumen": m.get("r"),
                    "url": m.get("u"), "categoria": k, "detectado": detectado, "texto_ok": bool(m.get("texto_ok"))})
                continue
            base = {"cuit": e["cuit"], "empresa": e["empresa"], "doc": m["id"], "fecha_presentacion": m["f"], "url": m.get("u"), "detectado": detectado}
            pagos = pagos_por_doc.get(m["id"])
            if not pagos:
                avisos.append({**base, "estructurado": False, "fecha_pago": None, "tipo_pago": None, "clase": None, "ticker": None, "monto_total": None, "moneda": None})
                continue
            for p in pagos:
                v = lambda c: (p.get(c) or {}).get("valor")  # noqa: E731
                avisos.append({**base, "estructurado": True, "fecha_pago": v("fecha_pago"), "tipo_pago": v("tipo_pago"), "clase": v("clase"), "ticker": v("ticker"),
                               "monto_total": v("monto_total"), "moneda": v("moneda")})
    orden = lambda x: (x.get("fecha") or x.get("fecha_presentacion") or "", int(x["doc"]))  # noqa: E731
    categorias = {k: sorted(v, key=orden, reverse=True) for k, v in sorted(por_categoria.items())}
    avisos.sort(key=lambda x: (orden(x), x.get("fecha_pago") or ""), reverse=True)
    n_hechos = sum(len(v) for v in categorias.values())
    return {
        "version": VERSION, "al": hoy.isoformat(), "desde": desde.isoformat(), "dias": dias,
        "resumen": {"hechos": n_hechos, "hechos_por_categoria": {k: len(v) for k, v in categorias.items()}, "avisos_pago": len({(a["cuit"], a["doc"]) for a in avisos}),
                    "pagos_estructurados": sum(1 for a in avisos if a["estructurado"]), "nuevos_en_esta_corrida": len(nuevos)},
        "hechos_por_categoria": categorias, "avisos_pago": avisos,
    }


def construir(almacen, empresas: list, hoy: dt.date | None = None, dias: int = DIAS) -> dict:
    """Escribe cnv/indice/novedades.json (solo si cambio) y devuelve el resumen."""
    informe = calcular(almacen, empresas, hoy, dias)
    alm.guardar_si_cambio(almacen, RUTA_NOVEDADES, _json(informe))
    return informe["resumen"]
