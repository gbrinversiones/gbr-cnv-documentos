"""Cuarta etapa: ratios por emisor y periodo, calculados SOLO con cifras de estados contables ya verificadas (cnv/financieros.py).

Regla central: un ratio se calcula unicamente si todas sus cifras son del mismo periodo (cierre, tipo y acumulado), la misma moneda y la
misma unidad/escala; si falta una cifra o algo no cierra, el ratio es null y se guarda el MOTIVO. Nunca se estima, no se anualiza y no se
completa una cifra con otra. Todo ratio lleva sus insumos (valor y pagina) y el doc_id de origen para poder auditarlo en cnv/financieros.

Salida: cnv/indice/ratios.json.gz
  {"version": 1, "formulas": {...}, "emisores": {cuit: {"empresa", "periodos": [
      {"periodo", "tipo", "acumulado", "balance", "moneda", "unidad", "escala", "doc", "periodo_verificado", "balance_coherente",
       "insumos": {campo: {"valor", "pagina"}}, "derivados": {"ebitda": ...}, "ratios": {nombre: valor | null}, "motivos": {nombre: motivo}}]}}}
"""
from . import almacen as alm
from .financieros import leer_financieros

VERSION = 1

# ratio -> cifras de entrada (todas deben existir y compartir contexto)
ENTRADAS = {
    "liquidez_corriente": ("activo_corriente", "pasivo_corriente"),
    "endeudamiento": ("pasivo_total", "patrimonio_neto"),
    "deuda_ebitda": ("deuda_financiera", "resultado_operativo", "depreciaciones_amortizaciones"),
    "margen_neto": ("resultado_neto", "ventas"),
    "roe": ("resultado_neto", "patrimonio_neto"),
    "cobertura_intereses": ("resultado_operativo", "intereses"),
}
# deuda/EBITDA y ROE mezclan un flujo (de todo el periodo) con una deuda o un patrimonio (a la fecha de cierre): solo son comparables con
# 12 meses de flujo. No se anualizan periodos parciales (seria estimar): en trimestres y semestres quedan null.
SOLO_ANUALES = ("deuda_ebitda", "roe")
FORMULAS = {
    "liquidez_corriente": "activo_corriente / pasivo_corriente",
    "endeudamiento": "pasivo_total / patrimonio_neto",
    "deuda_ebitda": "deuda_financiera / (resultado_operativo + |depreciaciones_amortizaciones|); solo periodos anuales",
    "margen_neto": "resultado_neto / ventas",
    "roe": "resultado_neto / patrimonio_neto (a la fecha de cierre, no promedio); solo periodos anuales",
    "cobertura_intereses": "resultado_operativo / |intereses|",
}


def contexto(p: dict, campo: str) -> dict | None:
    """El insumo `campo` de un periodo de financieros, con el contexto en que fue extraido; None si no figura."""
    c = (p.get("cifras") or {}).get(campo)
    if not c or not isinstance(c.get("valor"), (int, float)) or isinstance(c.get("valor"), bool):
        return None
    return {"valor": c["valor"], "pagina": c.get("pagina"), "periodo": p.get("periodo"), "tipo": p.get("tipo"), "acumulado": p.get("acumulado"),
            "moneda": (p.get("moneda") or "").strip().lower(), "escala": p.get("escala"), "doc": p.get("doc"), "balance": p.get("balance")}


def contexto_comun(insumos: list) -> str | None:
    """None si todos los insumos son del mismo periodo, moneda y unidad; si no, el motivo."""
    if len({(i["periodo"], i["tipo"], i["acumulado"]) for i in insumos}) > 1:
        return "periodos_distintos"
    if any(not i["moneda"] for i in insumos):
        return "moneda_desconocida"
    if len({i["moneda"] for i in insumos}) > 1:
        return "monedas_distintas"
    if any(i["escala"] not in (1, 1000, 1_000_000) for i in insumos):
        return "unidad_desconocida"
    if len({i["escala"] for i in insumos}) > 1:
        return "unidades_distintas"
    if len({i.get("balance") for i in insumos}) > 1:
        return "balances_distintos"  # consolidado vs individual
    return None


def _div(num: float, den: float, nombre_den: str) -> tuple:
    """(cociente, motivo): el denominador debe ser estrictamente positivo (cero -> division_por_cero, negativo -> {nombre}_negativo)."""
    if den == 0:
        return None, "division_por_cero"
    if den < 0:
        return None, f"{nombre_den}_negativo"
    return round(num / den, 6), None


def calcular_ratio(nombre: str, insumos: dict) -> tuple:
    """(valor | None, motivo | None, derivados). `insumos`: {campo: contexto(...)}; los campos que falten no estan en el dict."""
    campos = ENTRADAS[nombre]
    faltan = [c for c in campos if c not in insumos]
    if faltan:
        return None, "falta_cifra:" + ",".join(faltan), {}
    usados = [insumos[c] for c in campos]
    motivo = contexto_comun(usados)
    if motivo:
        return None, motivo, {}
    ref = usados[0]
    if nombre in SOLO_ANUALES and not (ref["tipo"] == "anual" and ref["acumulado"]):
        return None, "periodo_no_anual", {}
    v = {c: insumos[c]["valor"] for c in campos}
    if nombre == "liquidez_corriente":
        if v["activo_corriente"] < 0:
            return None, "activo_corriente_negativo", {}
        return (*_div(v["activo_corriente"], v["pasivo_corriente"], "pasivo_corriente"), {})
    if nombre == "endeudamiento":
        if v["pasivo_total"] < 0:
            return None, "pasivo_total_negativo", {}
        return (*_div(v["pasivo_total"], v["patrimonio_neto"], "patrimonio"), {})   # patrimonio <= 0: sin sentido economico, no se calcula
    if nombre == "margen_neto":
        return (*_div(v["resultado_neto"], v["ventas"], "ventas"), {})
    if nombre == "roe":
        return (*_div(v["resultado_neto"], v["patrimonio_neto"], "patrimonio"), {})
    if nombre == "cobertura_intereses":
        return (*_div(v["resultado_operativo"], abs(v["intereses"]), "intereses"), {})
    # deuda_ebitda
    if v["deuda_financiera"] < 0:
        return None, "deuda_financiera_negativa", {}
    ebitda = v["resultado_operativo"] + abs(v["depreciaciones_amortizaciones"])
    r, m = _div(v["deuda_financiera"], ebitda, "ebitda")
    return r, m, {"ebitda": ebitda}


def ratios_de_periodo(p: dict) -> dict:
    """Fila de un periodo de financieros: insumos, derivados, ratios y motivos de los que no se pudieron calcular."""
    insumos = {c: contexto(p, c) for c in {x for e in ENTRADAS.values() for x in e}}
    insumos = {c: i for c, i in insumos.items() if i}
    ratios, motivos, derivados = {}, {}, {}
    for nombre in ENTRADAS:
        valor, motivo, deriv = calcular_ratio(nombre, insumos)
        ratios[nombre] = valor
        derivados.update(deriv)
        if valor is None:
            motivos[nombre] = motivo
    usados = sorted({c for n in ENTRADAS for c in ENTRADAS[n] if c in insumos})
    return {
        "periodo": p.get("periodo"), "tipo": p.get("tipo"), "acumulado": p.get("acumulado"), "balance": p.get("balance", "desconocido"),
        "moneda": p.get("moneda"), "unidad": p.get("unidad"), "escala": p.get("escala"), "doc": p.get("doc"),
        "periodo_verificado": p.get("periodo_verificado"), "balance_coherente": p.get("balance_coherente"),
        "insumos": {c: {"valor": insumos[c]["valor"], "pagina": insumos[c]["pagina"]} for c in usados},
        "derivados": derivados, "ratios": ratios, "motivos": motivos,
    }


def periodos_vigentes(periodos: list) -> list:
    """Un periodo por (cierre, tipo, acumulado, balance): si dos documentos traen el mismo, queda el mas nuevo (igual que el indice de financieros)."""
    vistos = {}
    for p in periodos:
        clave = (p["periodo"], p["tipo"], p["acumulado"], p.get("balance", "desconocido"))
        if clave not in vistos or int(p["doc"]) > int(vistos[clave]["doc"]):
            vistos[clave] = p
    return sorted(vistos.values(), key=lambda p: (p["periodo"], p.get("balance", ""), int(p["doc"])), reverse=True)


def construir(almacen, empresas: list) -> dict:
    """Escribe cnv/indice/ratios.json.gz. Devuelve conteos: periodos, ratios calculados por nombre y motivos de los null."""
    import json

    emisores, stats = {}, {"emisores": 0, "periodos": 0, "calculados": {n: 0 for n in ENTRADAS}, "motivos": {}}
    for e in empresas:
        periodos = periodos_vigentes(leer_financieros(almacen, e["cuit"])["periodos"])
        if not periodos:
            continue
        filas = [ratios_de_periodo(p) for p in periodos]
        emisores[e["cuit"]] = {"empresa": e["empresa"], "periodos": filas}
        stats["emisores"] += 1
        stats["periodos"] += len(filas)
        for f in filas:
            for n, v in f["ratios"].items():
                if v is None:
                    m = f["motivos"][n].split(":")[0]
                    stats["motivos"][m] = stats["motivos"].get(m, 0) + 1
                else:
                    stats["calculados"][n] += 1
    salida = {"version": VERSION, "formulas": FORMULAS, "emisores": emisores}
    alm.guardar_si_cambio(almacen, "cnv/indice/ratios.json.gz", alm.comprimir(json.dumps(salida, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
    return stats
