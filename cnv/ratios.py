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
from .financieros import gana, leer_financieros, moneda_canonica

VERSION = 1

# ratio -> cifras de entrada (todas deben existir y compartir contexto)
ENTRADAS = {
    "liquidez_corriente": ("activo_corriente", "pasivo_corriente"),
    "endeudamiento": ("pasivo_total", "patrimonio_neto"),
    "deuda_ebitda": ("deuda_financiera", "resultado_operativo", "depreciaciones_amortizaciones"),
    "margen_neto": ("resultado_neto", "ventas"),
    "roe": ("resultado_neto", "patrimonio_neto"),
    "cobertura_intereses": ("resultado_operativo", "intereses"),
    # Fase 16 (cifras de flujo y deuda). La deuda total es corriente + no corriente (ambas deben figurar); no se mezcla con `deuda_financiera`.
    "deuda_neta_ebitda": ("deuda_financiera_corriente", "deuda_financiera_no_corriente", "caja_equivalentes", "resultado_operativo", "depreciaciones_amortizaciones"),
    "cobertura_deuda_cp": ("caja_equivalentes", "deuda_financiera_corriente"),
    "fcf_deuda": ("flujo_efectivo_operativo", "inversiones_capex", "deuda_financiera_corriente", "deuda_financiera_no_corriente"),
}
# deuda/EBITDA y ROE mezclan un flujo (de todo el periodo) con una deuda o un patrimonio (a la fecha de cierre): solo son comparables con
# 12 meses de flujo. No se anualizan periodos parciales (seria estimar): en trimestres y semestres quedan null.
SOLO_ANUALES = ("deuda_ebitda", "roe", "deuda_neta_ebitda", "fcf_deuda")
NO_NEGATIVOS_RATIO = ("caja_equivalentes", "deuda_financiera_corriente", "deuda_financiera_no_corriente")
FORMULAS = {
    "liquidez_corriente": "activo_corriente / pasivo_corriente",
    "endeudamiento": "pasivo_total / patrimonio_neto",
    "deuda_ebitda": "deuda_financiera / (resultado_operativo + |depreciaciones_amortizaciones|); solo periodos anuales",
    "margen_neto": "resultado_neto / ventas",
    "roe": "resultado_neto / patrimonio_neto (a la fecha de cierre, no promedio); solo periodos anuales",
    "cobertura_intereses": "resultado_operativo / |intereses|",
    "deuda_neta_ebitda": "(deuda_financiera_corriente + deuda_financiera_no_corriente - caja_equivalentes) / (resultado_operativo + |depreciaciones_amortizaciones|); solo periodos anuales; negativo = caja neta",
    "cobertura_deuda_cp": "caja_equivalentes / deuda_financiera_corriente (veces; ambas a la fecha de cierre)",
    "fcf_deuda": "(flujo_efectivo_operativo - |inversiones_capex|) / (deuda_financiera_corriente + deuda_financiera_no_corriente); solo periodos anuales",
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


def calcular_ratio(nombre: str, insumos: dict, sospechosas=frozenset()) -> tuple:
    """(valor | None, motivo | None, derivados). `insumos`: {campo: contexto(...)}; los campos que falten no estan en el dict.
    `sospechosas`: cifras que la auditoria (cnv/auditoria.py) marco como inconsistentes: un ratio que las usa queda null (`cifra_sospechosa`)."""
    campos = ENTRADAS[nombre]
    malas = [c for c in campos if c in sospechosas]
    if malas:
        return None, "cifra_sospechosa:" + ",".join(malas), {}
    faltan = [c for c in campos if c not in insumos]
    if faltan == ["intereses"]:
        return None, "sin_linea_intereses", {}          # el estado no trae una linea propia de intereses: es un dato que no existe, no un error
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
    negativos = [c for c in campos if c in NO_NEGATIVOS_RATIO and v[c] < 0]
    if negativos and nombre in ("deuda_neta_ebitda", "cobertura_deuda_cp", "fcf_deuda"):
        return None, f"{negativos[0]}_negativa", {}
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
    if nombre == "cobertura_deuda_cp":
        return (*_div(v["caja_equivalentes"], v["deuda_financiera_corriente"], "deuda_financiera_corriente"), {})
    if nombre == "deuda_neta_ebitda":
        deuda = v["deuda_financiera_corriente"] + v["deuda_financiera_no_corriente"]
        ebitda = v["resultado_operativo"] + abs(v["depreciaciones_amortizaciones"])
        if ebitda == 0:
            return None, "division_por_cero", {}
        if ebitda < 0:
            return None, "ebitda_negativo", {}
        return round((deuda - v["caja_equivalentes"]) / ebitda, 6), None, {"ebitda": ebitda, "deuda_total": deuda, "deuda_neta": deuda - v["caja_equivalentes"]}
    if nombre == "fcf_deuda":
        deuda = v["deuda_financiera_corriente"] + v["deuda_financiera_no_corriente"]
        fcf = v["flujo_efectivo_operativo"] - abs(v["inversiones_capex"])
        r, m = _div(fcf, deuda, "deuda_total")
        return r, m, {"fcf": fcf, "deuda_total": deuda}
    # deuda_ebitda
    if v["deuda_financiera"] < 0:
        return None, "deuda_financiera_negativa", {}
    ebitda = v["resultado_operativo"] + abs(v["depreciaciones_amortizaciones"])
    r, m = _div(v["deuda_financiera"], ebitda, "ebitda")
    return r, m, {"ebitda": ebitda}


def derivar_deuda_financiera(insumos: dict, sospechosas=frozenset()):
    """`deuda_financiera` = corriente + no corriente cuando el estado NO trae el total pero si los dos componentes (mismo periodo, moneda, unidad y balance,
    ninguno sospechoso). Es una suma de dos cifras verificadas, no una estimacion; queda marcada como derivada para que se vea de donde sale."""
    if "deuda_financiera" in insumos:
        return None
    c, n = insumos.get("deuda_financiera_corriente"), insumos.get("deuda_financiera_no_corriente")
    if not c or not n or {"deuda_financiera_corriente", "deuda_financiera_no_corriente"} & set(sospechosas) or contexto_comun([c, n]):
        return None
    return {**c, "valor": c["valor"] + n["valor"], "pagina": None, "derivado": "deuda_financiera_corriente + deuda_financiera_no_corriente"}


def ratios_de_periodo(p: dict, sospechosas=frozenset()) -> dict:
    """Fila de un periodo de financieros: insumos, derivados, ratios y motivos de los que no se pudieron calcular."""
    insumos = {c: contexto(p, c) for c in {x for e in ENTRADAS.values() for x in e}}
    insumos = {c: i for c, i in insumos.items() if i}
    ratios, motivos, derivados = {}, {}, {}
    total = derivar_deuda_financiera(insumos, sospechosas)
    if total:
        insumos["deuda_financiera"] = total
        derivados["deuda_financiera"] = total["valor"]
    for nombre in ENTRADAS:
        valor, motivo, deriv = calcular_ratio(nombre, insumos, sospechosas)
        ratios[nombre] = valor
        derivados.update(deriv)
        if valor is None:
            motivos[nombre] = motivo
    usados = sorted({c for n in ENTRADAS for c in ENTRADAS[n] if c in insumos and c not in sospechosas})
    fila = {
        "periodo": p.get("periodo"), "tipo": p.get("tipo"), "acumulado": p.get("acumulado"), "balance": p.get("balance", "desconocido"),
        "moneda": p.get("moneda"), "unidad": p.get("unidad"), "escala": p.get("escala"), "doc": p.get("doc"),
        "periodo_verificado": p.get("periodo_verificado"), "balance_coherente": p.get("balance_coherente"),
        **({"ocr": True} if p.get("ocr") else {}),       # cifras leidas por OCR (ver financieros.rechazo_ocr)
        "insumos": {c: {"valor": insumos[c]["valor"], "pagina": insumos[c]["pagina"], **({"derivado": insumos[c]["derivado"]} if insumos[c].get("derivado") else {})} for c in usados},
        "derivados": derivados, "ratios": ratios, "motivos": motivos, "ratios_calculados": sum(v is not None for v in ratios.values()),
    }
    malas = sorted(c for c in sospechosas if c in insumos)
    if malas:
        fila["sospechosas"] = malas      # cifras presentes pero marcadas por la auditoria: no se usaron
    return fila


def periodos_vigentes(periodos: list) -> list:
    """Un periodo por (cierre, tipo, acumulado, balance, moneda): si dos documentos traen el mismo, queda el mas nuevo (igual que el indice de financieros).
    El consolidado en dolares y el consolidado en pesos del mismo cierre son dos filas (antes la mas vieja se perdia)."""
    vistos = {}
    for p in periodos:
        clave = (p["periodo"], p["tipo"], p["acumulado"], p.get("balance", "desconocido"), moneda_canonica(p.get("moneda")))
        if clave not in vistos or gana(p, vistos[clave]):     # el texto del PDF antes que el leido por OCR
            vistos[clave] = p
    return sorted(vistos.values(), key=lambda p: (p["periodo"], p.get("balance", ""), int(p["doc"])), reverse=True)


RANGO_BALANCE = {"consolidado": 0, "individual": 1}


def seleccionar_ficha(filas: list) -> list:
    """UNA fila por cierre (periodo, tipo, acumulado) para mostrar en la ficha, con las demas como `alternativas` (balance, moneda, doc y ratios calculados).
    Orden de preferencia: 1) CONSOLIDADO antes que individual (desconocido al final); 2) la que tenga mas ratios calculables; 3) en dolares (moneda funcional
    habitual de los emisores que reportan en dos monedas); 4) el documento mas nuevo. Cada fila conserva `balance` y `moneda` explicitos."""
    grupos = {}
    for f in filas:
        grupos.setdefault((f["periodo"], f["tipo"], f["acumulado"]), []).append(f)
    salida = []
    for fs in grupos.values():
        orden = sorted(fs, key=lambda f: (RANGO_BALANCE.get(f.get("balance"), 2), -f["ratios_calculados"], 0 if moneda_canonica(f.get("moneda")) == "dolares" else 1, -int(f["doc"])))
        salida.append({**orden[0], "alternativas": [{"balance": f.get("balance"), "moneda": f.get("moneda"), "doc": f["doc"], "ratios_calculados": f["ratios_calculados"]} for f in orden[1:]]})
    return sorted(salida, key=lambda f: (f["periodo"], f["tipo"] or ""), reverse=True)


def construir(almacen, empresas: list) -> dict:
    """Escribe cnv/indice/ratios.json.gz. Devuelve conteos: periodos, ratios calculados por nombre y motivos de los null."""
    import json

    from .auditoria import leer_sospechosas

    auditadas = leer_sospechosas(almacen)
    emisores, stats = {}, {"emisores": 0, "periodos": 0, "calculados": {n: 0 for n in ENTRADAS}, "motivos": {}}
    for e in empresas:
        periodos = periodos_vigentes(leer_financieros(almacen, e["cuit"])["periodos"])
        if not periodos:
            continue
        filas = [ratios_de_periodo(p, auditadas.get(e["cuit"], {}).get(p["doc"], frozenset())) for p in periodos]
        ficha = seleccionar_ficha(filas)
        emisores[e["cuit"]] = {"empresa": e["empresa"], "periodos": filas, "ficha": ficha}
        stats["emisores"] += 1
        stats["periodos"] += len(filas)
        stats["ficha"] = stats.get("ficha", 0) + len(ficha)
        for f in filas:
            for n, v in f["ratios"].items():
                if v is None:
                    m = f["motivos"][n].split(":")[0]
                    stats["motivos"][m] = stats["motivos"].get(m, 0) + 1
                else:
                    stats["calculados"][n] += 1
    salida = {"version": VERSION, "formulas": FORMULAS, "auditoria_cifras": bool(auditadas), "emisores": emisores}
    alm.guardar_si_cambio(almacen, "cnv/indice/ratios.json.gz", alm.comprimir(json.dumps(salida, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
    return stats
