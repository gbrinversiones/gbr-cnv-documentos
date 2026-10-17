"""Calidad de la base: auditoria contable de las cifras ya verificadas de cnv/financieros.

Una cifra puede estar bien respaldada por su cita (esta en el documento, el numero esta en la cita) y aun asi estar MAL: la fila equivocada, la
columna comparativa, una escala mal declarada. Esta etapa no usa el modelo ni la red: cruza las cifras entre si (dentro de un periodo y entre
periodos del mismo emisor) y marca las que no cierran. Las cifras marcadas "sospechosas" NO se usan en cnv/ratios.py (motivo `cifra_sospechosa`).

Reglas (cada hallazgo lleva `regla`, `severidad`, `campos` y `detalle`):
  balance_no_cuadra                  activo_total != pasivo_total + patrimonio_neto fuera del 1%
  resultado_neto_mayor_que_ventas    resultado_neto > ventas (ventas > 0); idem resultado_operativo
  signo_raro                         ventas, activos, pasivos, efectivo, deuda o depreciaciones negativos
  parte_mayor_que_el_todo            activo_corriente > activo_total, pasivo_corriente > pasivo_total, efectivo > activo_total, deuda_financiera > pasivo_total
  salto_entre_periodos               una cifra (en unidades absolutas) cambia mas de 50x entre dos periodos consecutivos del mismo emisor
  unidad_incoherente                 el salto de arriba coincide con un cambio de escala declarada (miles <-> millones <-> unidades)
  escala_cambia / moneda_cambia      (info) el emisor cambio de unidad o de moneda entre periodos; no se marca ninguna cifra
Severidad "sospechosa" = la cifra queda fuera de los ratios; "info" = solo se informa.

Salida: cnv/indice/auditoria_cifras.json.gz
  {"version": 1, "resumen": {...}, "hallazgos": [{cuit, empresa, doc, periodo, tipo, balance, regla, severidad, campos, detalle}],
   "sospechosas": {cuit: {doc_id: [campo, ...]}}}
"""
import json
import math

from . import almacen as alm
from .financieros import leer_financieros

VERSION = 1
TOLERANCIA = 0.01           # 1% para el balance y para "parte mayor que el todo"
SALTO_MAXIMO = 50           # veces entre periodos consecutivos
NO_NEGATIVOS = ("ventas", "activo_total", "pasivo_total", "efectivo", "deuda_financiera", "activo_corriente", "pasivo_corriente", "depreciaciones_amortizaciones")
PARTES = (("activo_corriente", "activo_total"), ("pasivo_corriente", "pasivo_total"), ("efectivo", "activo_total"), ("deuda_financiera", "pasivo_total"))
EN_SERIE = ("ventas", "activo_total", "pasivo_total", "patrimonio_neto", "deuda_financiera", "efectivo", "activo_corriente", "pasivo_corriente")   # cifras comparables entre periodos
ESCALAS = (1, 1_000, 1_000_000)


def _v(p: dict, campo: str):
    c = (p.get("cifras") or {}).get(campo)
    v = c.get("valor") if isinstance(c, dict) else None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _abs(p: dict, campo: str):
    """Valor en unidades absolutas (valor tal cual figura x escala declarada)."""
    v, e = _v(p, campo), p.get("escala")
    return v * e if v is not None and e in ESCALAS else None


def _h(p: dict, regla: str, campos: list, detalle: str, severidad: str = "sospechosa") -> dict:
    return {"doc": p.get("doc"), "periodo": p.get("periodo"), "tipo": p.get("tipo"), "balance": p.get("balance", "desconocido"), "regla": regla,
            "severidad": severidad, "campos": sorted(campos), "detalle": detalle}


def auditar_periodo(p: dict) -> list:
    """Hallazgos que se ven dentro de un solo periodo (no hace falta comparar con otros)."""
    hs = []
    a, pa, pn = _v(p, "activo_total"), _v(p, "pasivo_total"), _v(p, "patrimonio_neto")
    if a is not None and pa is not None and pn is not None and a:
        dif = abs(a - (pa + pn))
        if dif > TOLERANCIA * abs(a):
            hs.append(_h(p, "balance_no_cuadra", ["activo_total", "pasivo_total", "patrimonio_neto"],
                         f"activo {a:g} vs pasivo + patrimonio {pa + pn:g}: diferencia de {dif / abs(a):.1%} (tolerancia {TOLERANCIA:.0%})"))
    ventas = _v(p, "ventas")
    for campo in ("resultado_neto", "resultado_operativo"):
        r = _v(p, campo)
        if ventas is not None and ventas > 0 and r is not None and r > ventas:
            hs.append(_h(p, f"{campo}_mayor_que_ventas", [campo, "ventas"], f"{campo} {r:g} supera a las ventas {ventas:g}"))
    for campo in NO_NEGATIVOS:
        v = _v(p, campo)
        if v is not None and v < 0:
            hs.append(_h(p, "signo_raro", [campo], f"{campo} = {v:g} es negativo"))
    for parte, todo in PARTES:
        x, y = _v(p, parte), _v(p, todo)
        if x is not None and y is not None and x >= 0 and y > 0 and x > y * (1 + TOLERANCIA):
            hs.append(_h(p, "parte_mayor_que_el_todo", [parte, todo], f"{parte} {x:g} supera a {todo} {y:g}"))
    return hs


def _log_mediana(valores: list) -> float:
    xs = sorted(math.log10(v) for v in valores)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def auditar_serie(periodos: list) -> list:
    """Hallazgos entre periodos consecutivos de UN emisor (mismo tipo de balance y misma moneda), de mas viejo a mas nuevo."""
    hs = []
    grupos = {}
    for p in periodos:
        grupos.setdefault((p.get("balance", "desconocido"), (p.get("moneda") or "").strip().lower()), []).append(p)
    por_balance = {}
    for p in periodos:
        por_balance.setdefault(p.get("balance", "desconocido"), []).append(p)
    # cambios de moneda / escala entre periodos consecutivos del mismo balance (informativo)
    for bal, ps in por_balance.items():
        ps = sorted(ps, key=lambda p: (p["periodo"], int(p["doc"])))
        for ant, nue in zip(ps, ps[1:]):
            if (ant.get("moneda") or "").strip().lower() != (nue.get("moneda") or "").strip().lower():
                hs.append(_h(nue, "moneda_cambia", [], f"{ant.get('moneda')} ({ant['periodo']}) -> {nue.get('moneda')} ({nue['periodo']})", "info"))
            elif ant.get("escala") != nue.get("escala"):
                hs.append(_h(nue, "escala_cambia", [], f"escala {ant.get('escala')} ({ant['periodo']}) -> {nue.get('escala')} ({nue['periodo']})", "info"))
    for (bal, moneda), ps in grupos.items():
        ps = sorted(ps, key=lambda p: (p["periodo"], int(p["doc"])))
        for campo in EN_SERIE:
            serie = [(p, _abs(p, campo)) for p in ps if _abs(p, campo)]
            serie = [(p, x) for p, x in serie if x > 0]
            if len(serie) < 2:
                continue
            mediana = _log_mediana([x for _, x in serie]) if len(serie) >= 3 else None
            for (p0, x0), (p1, x1) in zip(serie, serie[1:]):
                razon = max(x0, x1) / min(x0, x1)
                if razon <= SALTO_MAXIMO:
                    continue
                if mediana is None:
                    culpables = [p0, p1]          # con dos puntos no se puede saber cual esta mal: se marcan los dos
                else:
                    culpables = [p0 if abs(math.log10(x0) - mediana) > abs(math.log10(x1) - mediana) else p1]
                cambio_escala = p0.get("escala") != p1.get("escala")
                regla = "unidad_incoherente" if cambio_escala else "salto_entre_periodos"
                detalle = f"{campo}: {x0:g} ({p0['periodo']}, escala {p0.get('escala')}) -> {x1:g} ({p1['periodo']}, escala {p1.get('escala')}), {razon:.0f} veces"
                hs += [_h(c, regla, [campo], detalle) for c in culpables]
    return hs


def auditar(almacen, empresas: list) -> dict:
    """Informe completo (dict serializable) sobre todos los periodos de financieros."""
    hallazgos, sospechosas, periodos_total = [], {}, 0
    for e in empresas:
        periodos = [p for p in leer_financieros(almacen, e["cuit"])["periodos"] if p.get("periodo") and p.get("doc") is not None]
        periodos_total += len(periodos)
        hs = [h for p in periodos for h in auditar_periodo(p)] + auditar_serie(periodos)
        vistos = set()
        for h in hs:
            clave = (h["doc"], h["regla"], tuple(h["campos"]), h["detalle"])
            if clave in vistos:
                continue
            vistos.add(clave)
            hallazgos.append({"cuit": e["cuit"], "empresa": e["empresa"], **h})
            if h["severidad"] == "sospechosa":
                s = set(sospechosas.setdefault(e["cuit"], {}).get(h["doc"], []))
                sospechosas[e["cuit"]][h["doc"]] = sorted(s | set(h["campos"]))
    hallazgos.sort(key=lambda h: (h["cuit"], h["periodo"], h["regla"], h["doc"]), reverse=False)
    por_regla = {}
    for h in hallazgos:
        por_regla[h["regla"]] = por_regla.get(h["regla"], 0) + 1
    return {
        "version": VERSION,
        "resumen": {"periodos": periodos_total, "hallazgos": len(hallazgos), "por_regla": dict(sorted(por_regla.items())),
                    "periodos_con_cifras_sospechosas": sum(len(d) for d in sospechosas.values()), "cifras_sospechosas": sum(len(c) for d in sospechosas.values() for c in d.values())},
        "hallazgos": hallazgos, "sospechosas": {c: dict(sorted(d.items())) for c, d in sorted(sospechosas.items())},
    }


def construir(almacen, empresas: list) -> dict:
    """Escribe cnv/indice/auditoria_cifras.json.gz (sin reescribirlo si no cambio) y devuelve el resumen."""
    informe = auditar(almacen, empresas)
    alm.guardar_si_cambio(almacen, "cnv/indice/auditoria_cifras.json.gz", alm.comprimir(json.dumps(informe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
    return informe["resumen"]


def leer_sospechosas(almacen) -> dict:
    """{cuit: {doc_id: {campo, ...}}} de la ultima auditoria; {} si todavia no se corrio (o esta ilegible: no se usa nada a medias)."""
    import gzip

    crudo = almacen.leer("cnv/indice/auditoria_cifras.json.gz")
    if not crudo:
        return {}
    try:
        d = json.loads(gzip.decompress(crudo))
        return {c: {doc: set(campos) for doc, campos in docs.items()} for c, docs in d["sospechosas"].items()} if d.get("version") == VERSION else {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
