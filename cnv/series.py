"""Series historicas por emisor para graficos: cnv/indice/series.json.gz. Sin modelo ni red; nada estimado y cada punto trae el `doc` de origen.

Una serie solo tiene puntos COMPARABLES: para cada emisor y cada `tipo` de periodo (anual, trimestral, semestral: nunca mezclados) se elige UNA "pista" =
(balance, moneda, acumulado) y la serie se arma solo con los periodos de esa pista. La pista se elige igual que la ficha de cnv/ratios.py: balance
consolidado antes que individual, despues la que tenga mas valores de ratio calculados, despues en dolares, despues acumulado. Los periodos de otras pistas
(otro balance u otra moneda) no se mezclan: se cuentan en `omitidos`.

Formato:
  {"version": 1, "emisores": {cuit: {"empresa", "series": {tipo: {
      "balance", "moneda", "acumulado", "periodos": n,
      "ratios": {ratio: [{"periodo", "tipo", "valor", "balance", "moneda", "doc"}, ...]},       # cierre ascendente, los ultimos 12, sin nulos
      "base": {campo: {"unidad", "escala", "puntos": [{"periodo", "valor", "doc", "pagina"}, ...]}},
      "omitidos": {"otra_pista": n, "escala_distinta": n}}}}}}
Las cifras base (ventas, resultado_neto, patrimonio_neto, deuda_total, caja) van en la unidad ORIGINAL del documento (`escala` y `unidad`): si la escala cambia dentro de
la serie, solo quedan los puntos con la escala del ultimo (no se convierte ni se mezcla). `deuda_total` es el total `deuda_financiera` del estado o, si no figura,
corriente + no corriente (marcado `derivado`); `caja` es `caja_equivalentes` o, en estados viejos, `efectivo` (marcado en `campo`). Las cifras que la auditoria marco
como sospechosas no entran.
"""
import json

from . import almacen as alm
from .auditoria import leer_sospechosas
from .financieros import leer_financieros, moneda_canonica
from .ratios import RANGO_BALANCE, periodos_vigentes, ratios_de_periodo

VERSION = 1
MAX_PERIODOS = 12
RATIOS = ("liquidez_corriente", "endeudamiento", "margen_neto", "deuda_neta_ebitda", "cobertura_deuda_cp", "fcf_deuda", "roe")
TIPOS = ("anual", "semestral", "trimestral")
RUTA = "cnv/indice/series.json.gz"


def _valor(p: dict, campo: str, sospechosas) -> tuple | None:
    """(valor, pagina) de una cifra del periodo si figura y no es sospechosa."""
    c = (p.get("cifras") or {}).get(campo)
    if campo in sospechosas or not c or isinstance(c.get("valor"), bool) or not isinstance(c.get("valor"), (int, float)):
        return None
    return c["valor"], c.get("pagina")


def cifras_base(p: dict, sospechosas=frozenset()) -> dict:
    """{campo: {"valor", "pagina", ...}} de las cinco cifras base de un periodo, con la regla de deuda_total y caja."""
    salida = {}
    for campo in ("ventas", "resultado_neto", "patrimonio_neto"):
        v = _valor(p, campo, sospechosas)
        if v:
            salida[campo] = {"valor": v[0], "pagina": v[1]}
    total = _valor(p, "deuda_financiera", sospechosas)
    corriente, no_corriente = _valor(p, "deuda_financiera_corriente", sospechosas), _valor(p, "deuda_financiera_no_corriente", sospechosas)
    if total:
        salida["deuda_total"] = {"valor": total[0], "pagina": total[1]}
    elif corriente and no_corriente:
        salida["deuda_total"] = {"valor": corriente[0] + no_corriente[0], "pagina": None, "derivado": "deuda_financiera_corriente + deuda_financiera_no_corriente"}
    for campo in ("caja_equivalentes", "efectivo"):
        v = _valor(p, campo, sospechosas)
        if v:
            salida["caja"] = {"valor": v[0], "pagina": v[1], "campo": campo}
            break
    return salida


def _pista(f: dict) -> tuple:
    return (f.get("acumulado"), f.get("balance", "desconocido"), moneda_canonica(f.get("moneda")))


def elegir_pista(filas: list) -> tuple:
    """La pista (acumulado, balance, moneda) de un conjunto de filas del mismo tipo: consolidado primero, despues mas valores de ratio, despues dolares, despues acumulado."""
    cuenta = {}
    for f in filas:
        k = _pista(f)
        cuenta[k] = cuenta.get(k, 0) + sum(f["ratios"].get(r) is not None for r in RATIOS)
    return min(cuenta, key=lambda k: (RANGO_BALANCE.get(k[1], 2), -cuenta[k], 0 if k[2] == "dolares" else 1, 0 if k[0] else 1, str(k)))


def serie_de_tipo(filas: list, periodos: dict, sospechosas_de) -> dict | None:
    """Serie de UN tipo de periodo. `periodos`: {(doc, moneda canonica): periodo de financieros}; `sospechosas_de(doc)`: cifras marcadas por la auditoria."""
    pista = elegir_pista(filas)
    propias = sorted((f for f in filas if _pista(f) == pista), key=lambda f: f["periodo"])
    ratios = {}
    for r in RATIOS:
        puntos = [{"periodo": f["periodo"], "tipo": f["tipo"], "valor": f["ratios"][r], "balance": f.get("balance"), "moneda": f.get("moneda"), "doc": f["doc"]}
                  for f in propias if f["ratios"].get(r) is not None]
        if puntos:
            ratios[r] = puntos[-MAX_PERIODOS:]
    base, escala_distinta = {}, 0
    por_campo = {}
    for f in propias:
        p = periodos.get((f["doc"], moneda_canonica(f.get("moneda"))))
        if p is None:
            continue
        for campo, d in cifras_base(p, sospechosas_de(f["doc"])).items():
            por_campo.setdefault(campo, []).append({"periodo": f["periodo"], "valor": d["valor"], "doc": f["doc"], "pagina": d["pagina"], "escala": p.get("escala"), "unidad": p.get("unidad"),
                                                   **{k: d[k] for k in ("derivado", "campo") if k in d}})
    for campo, puntos in por_campo.items():
        ultima = puntos[-1]
        validos = [x for x in puntos if x["escala"] == ultima["escala"]]
        escala_distinta += len(puntos) - len(validos)
        base[campo] = {"unidad": ultima["unidad"], "escala": ultima["escala"],
                       "puntos": [{k: v for k, v in x.items() if k not in ("escala", "unidad")} for x in validos[-MAX_PERIODOS:]]}
    if not ratios and not base:
        return None
    return {"balance": pista[1], "moneda": propias[-1].get("moneda"), "acumulado": pista[0], "periodos": len(propias), "ratios": ratios, "base": base,
            "omitidos": {"otra_pista": len(filas) - len(propias), "escala_distinta": escala_distinta}}


def serie_de_emisor(periodos_fin: list, sospechosas_por_doc: dict) -> dict:
    """{tipo: serie} de un emisor a partir de los periodos de financieros."""
    vigentes = periodos_vigentes(periodos_fin)
    filas = [ratios_de_periodo(p, sospechosas_por_doc.get(p["doc"], frozenset())) for p in vigentes]
    periodos = {(p["doc"], moneda_canonica(p.get("moneda"))): p for p in vigentes}
    salida = {}
    for tipo in TIPOS:
        del_tipo = [f for f in filas if f["tipo"] == tipo]
        if del_tipo:
            s = serie_de_tipo(del_tipo, periodos, lambda doc: sospechosas_por_doc.get(doc, frozenset()))
            if s:
                salida[tipo] = s
    return salida


def construir(almacen, empresas: list) -> dict:
    """Escribe cnv/indice/series.json.gz (sin reescribirlo si no cambio). Devuelve {"emisores", "series", "puntos"}."""
    sospechosas = leer_sospechosas(almacen)
    emisores, n_series, n_puntos = {}, 0, 0
    for e in sorted(empresas, key=lambda e: (e["empresa"], e["cuit"])):
        series = serie_de_emisor(leer_financieros(almacen, e["cuit"])["periodos"], sospechosas.get(e["cuit"], {}))
        if not series:
            continue
        emisores[e["cuit"]] = {"empresa": e["empresa"], "series": series}
        n_series += len(series)
        n_puntos += sum(len(p) for s in series.values() for p in s["ratios"].values())
    salida = {"version": VERSION, "emisores": emisores}
    alm.guardar_si_cambio(almacen, RUTA, alm.comprimir(json.dumps(salida, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
    return {"emisores": len(emisores), "series": n_series, "puntos": n_puntos}
