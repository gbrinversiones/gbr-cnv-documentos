"""Muestra de calidad de las cifras extraidas: una revision cruzada de N estados contables ya procesados (30 por defecto), sin modelo ni red.

Para cada periodo de la muestra se comprueba lo que se puede comprobar SIN volver a leer el PDF:
  - balance: activo = pasivo + patrimonio (tolerancia 1 %),
  - ratios razonables: cada ratio calculado cae dentro de un rango economicamente plausible (RANGOS; fuera del rango NO es un error, es «mirar este»),
  - unidad: escala y moneda declaradas coinciden con las del periodo anterior del mismo emisor y balance (cambios de escala/moneda son la causa mas
    comun de cifras que «verifican» contra su cita y aun asi estan mal),
  - respaldo: toda cifra trae cita y pagina,
  - auditoria: hallazgos `sospechosa` de cnv/auditoria.py sobre ese periodo.
La muestra es DETERMINISTA (ordena por un hash del documento y toma los primeros N): el mismo bucket da la misma muestra, asi dos personas ven lo mismo.
El informe tambien cuenta los patrones de error de TODA la base (reglas de la auditoria) para ver cuales son los mas frecuentes; la revision humana
de la muestra contra el PDF (que es lo que valida el extractor) se anota a mano en docs/CALIDAD.md.
"""
import hashlib

from . import almacen as alm
from . import auditoria as aud
from .financieros import leer_financieros
from .ratios import periodos_vigentes, ratios_de_periodo

N_DEFECTO = 30
# rangos economicamente plausibles para empresas operativas; fuera de rango = revisar (puede ser real: patrimonio casi nulo, EBITDA minimo...)
RANGOS = {"liquidez_corriente": (0.05, 20), "endeudamiento": (0, 60), "deuda_ebitda": (0, 40), "margen_neto": (-3, 1), "roe": (-3, 3), "cobertura_intereses": (-100, 1000),
          "deuda_neta_ebitda": (-30, 40), "cobertura_deuda_cp": (0, 100), "fcf_deuda": (-5, 5)}


def clave_orden(cuit: str, doc: str) -> str:
    return hashlib.sha1(f"{cuit}:{doc}".encode("utf-8")).hexdigest()


def revisar_periodo(p: dict, anterior: dict | None, sospechosas=frozenset()) -> dict:
    """{"checks": {nombre: {"ok": bool|None, "detalle": str}}, "veredicto": ok|revisar|sospechoso}. `ok` None = no se pudo comprobar (faltan cifras)."""
    checks = {}
    a, pa, pn = aud._v(p, "activo_total"), aud._v(p, "pasivo_total"), aud._v(p, "patrimonio_neto")
    if a and pa is not None and pn is not None:
        dif = abs(a - (pa + pn)) / abs(a)
        checks["balance"] = {"ok": dif <= aud.TOLERANCIA, "detalle": f"diferencia {dif:.2%} (tolerancia {aud.TOLERANCIA:.0%})"}
    else:
        checks["balance"] = {"ok": None, "detalle": "faltan activo, pasivo o patrimonio"}
    fuera = []
    ratios = ratios_de_periodo(p, sospechosas)["ratios"]
    for nombre, v in ratios.items():
        lo, hi = RANGOS.get(nombre, (None, None))
        if v is not None and lo is not None and not lo <= v <= hi:
            fuera.append(f"{nombre}={v:g} fuera de [{lo}, {hi}]")
    calculados = sum(v is not None for v in ratios.values())
    checks["ratios"] = {"ok": (not fuera) if calculados else None, "detalle": "; ".join(fuera) or (f"{calculados} ratios en rango" if calculados else "ningun ratio calculable")}
    if anterior is None:
        checks["unidad"] = {"ok": None, "detalle": "sin periodo anterior del mismo emisor y balance"}
    else:
        misma_m = aud.moneda_canonica(p.get("moneda")) == aud.moneda_canonica(anterior.get("moneda"))
        misma_e = p.get("escala") == anterior.get("escala")
        checks["unidad"] = {"ok": misma_m and misma_e, "detalle": f"escala {anterior.get('escala')} -> {p.get('escala')}; moneda {anterior.get('moneda')} -> {p.get('moneda')} (periodo anterior {anterior.get('periodo')})"}
    sin_cita = sorted(c for c, x in (p.get("cifras") or {}).items() if not (x.get("cita") and x.get("pagina")))
    checks["respaldo"] = {"ok": not sin_cita, "detalle": f"sin cita o pagina: {', '.join(sin_cita)}" if sin_cita else f"{len(p.get('cifras') or {})} cifras con cita y pagina"}
    marcadas = sorted(h["regla"] for h in aud.auditar_periodo(p) if h["severidad"] == "sospechosa")
    checks["auditoria"] = {"ok": not marcadas, "detalle": ", ".join(marcadas) or "sin hallazgos"}
    veredicto = "sospechoso" if (marcadas or checks["balance"]["ok"] is False or checks["respaldo"]["ok"] is False) else "revisar" if any(c["ok"] is False for c in checks.values()) else "ok"
    return {"checks": checks, "veredicto": veredicto}


def recolectar(almacen, empresas: list) -> list:
    """[(empresa, periodo, anterior)] de todos los periodos vigentes con cifras; `anterior` = el periodo inmediato anterior del mismo emisor y balance."""
    salida = []
    for e in empresas:
        ps = [p for p in periodos_vigentes(leer_financieros(almacen, e["cuit"])["periodos"]) if p.get("cifras")]
        por_balance = {}
        for p in sorted(ps, key=lambda p: (p["periodo"], int(p["doc"]))):
            por_balance.setdefault(p.get("balance", "desconocido"), []).append(p)
        for lista in por_balance.values():
            for i, p in enumerate(lista):
                salida.append((e, p, lista[i - 1] if i else None))
    return salida


def muestrear(todos: list, n: int = N_DEFECTO) -> list:
    return sorted(todos, key=lambda x: clave_orden(x[0]["cuit"], x[1]["doc"]))[:n]


def calcular(almacen, empresas: list, n: int = N_DEFECTO) -> dict:
    todos = recolectar(almacen, empresas)
    sospechosas = aud.leer_sospechosas(almacen)
    filas = []
    for e, p, ant in muestrear(todos, n):
        r = revisar_periodo(p, ant, sospechosas.get(e["cuit"], {}).get(p["doc"], frozenset()))
        filas.append({"empresa": e["empresa"], "cuit": e["cuit"], "doc": p["doc"], "periodo": p["periodo"], "tipo": p.get("tipo"), "balance": p.get("balance", "desconocido"), **r})
    veredictos = {}
    for f in filas:
        veredictos[f["veredicto"]] = veredictos.get(f["veredicto"], 0) + 1
    patrones = aud.auditar(almacen, empresas)["resumen"]["por_regla"]
    return {"periodos_en_base": len(todos), "muestra": len(filas), "veredictos": dict(sorted(veredictos.items())), "patrones_en_toda_la_base": dict(sorted(patrones.items(), key=lambda kv: (-kv[1], kv[0]))), "filas": filas}


def a_markdown(inf: dict) -> str:
    L = [f"## Muestra de calidad de cifras ({inf['muestra']} de {inf['periodos_en_base']} periodos)", "",
         "Veredictos: " + (", ".join(f"**{v}** {n}" for v, n in inf["veredictos"].items()) or "sin periodos"), "",
         "### Patrones de la auditoría en toda la base (de más a menos frecuente)", "", "| Regla | Hallazgos |", "|---|--:|"]
    L += [f"| {r} | {n} |" for r, n in inf["patrones_en_toda_la_base"].items()] or ["| (ninguno) | 0 |"]
    L += ["", "### Muestra", "", "| Veredicto | Empresa | Periodo | Balance | Cuadra | Ratios | Unidad | Respaldo | Auditoría |", "|---|---|---|---|---|---|---|---|---|"]
    marca = {True: "✓", False: "✗", None: "·"}
    for f in inf["filas"]:
        c = f["checks"]
        L.append(f"| {f['veredicto']} | {f['empresa']} #{f['doc']} | {f['periodo']} {f['tipo']} | {f['balance']} | {marca[c['balance']['ok']]} | {marca[c['ratios']['ok']]} | {marca[c['unidad']['ok']]} | {marca[c['respaldo']['ok']]} | {marca[c['auditoria']['ok']]} |")
    problemas = [f for f in inf["filas"] if f["veredicto"] != "ok"]
    L += ["", f"### Detalle de lo que no está ok ({len(problemas)})"]
    for f in problemas:
        L.append(f"- **{f['empresa']} #{f['doc']} ({f['periodo']})**: " + "; ".join(f"{k}: {c['detalle']}" for k, c in f["checks"].items() if c["ok"] is False))
    if not problemas:
        L.append("- nada")
    L += ["", "_✓ comprobado · ✗ no cierra · · no se pudo comprobar. La muestra es determinista; validar contra el PDF a mano lo que figure en ✗ o «sospechoso»._", ""]
    return "\n".join(L)
