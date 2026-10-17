"""Ampliar el universo de emisores con los de las ONs del tablero de BYMA (payload `ons_byma.json` del repo de assets).

`python -m cnv universo --sugerir-desde-byma ons_byma.json` lista los emisores del payload, descarta los que ya estan en data/universo.csv
(coincidencia EXACTA de razon social normalizada) y, para cada emisor nuevo, busca su CUIT en el autocompletado de la CNV.

Regla de oro (nada se adivina): un CUIT solo se acepta si
  1. una razon social del autocompletado coincide EXACTAMENTE con la de BYMA (misma clave normalizada, ver `clave`), y esa razon corresponde a UN solo CUIT,
  2. el CUIT tiene digito verificador valido, y
  3. la pagina de la CNV de ese CUIT muestra tambien una razon social con la misma clave.
Todo lo demas (sin coincidencia exacta, varias, una pagina que no confirma, la CNV que no responde) va al informe
`informes/emisores_por_revisar.json` con sus candidatos: NUNCA al CSV. Aun lo confirmado solo se escribe en los CSV con `--escribir`, y el
workflow lo hace en una rama + pull request: nada entra a data/universo.csv (y por lo tanto a la sincronizacion) hasta que una persona lo mergea.

De BYMA solo se lee ticker, emisor y fecha de vencimiento (datos publicos del instrumento); los precios y tasas no se usan ni se guardan.
"""
import csv
import json
import re
import time

from . import universo as uni
from .salud import cuit_valido

RUTA_REVISAR = "informes/emisores_por_revisar.json"
ORIGEN_NUEVO = "BYMA-ON"
FUENTE_UNIVERSO = "universo_exacto_byma"          # el emisor ya estaba en universo.csv con la misma razon social normalizada
FUENTE_CNV = "cnv_autocompletado_exacto"          # el CUIT salio del autocompletado + pagina de la CNV (coincidencia exacta)
COLUMNAS_ONS = ["ticker", "emisor", "descripcion", "vence", "cuit", "fuente_cuit"]


# Forma societaria que se quita del FINAL de la razon social (nada de "holding", "grupo", "argentina"...: son parte del nombre y distinguen empresas
# distintas, p. ej. EDESA S.A. y EDESA HOLDING S.A.). Las iniciales sueltas del final (S.A.C.I.F.I.A.) tambien son forma societaria.
_FORMA_SOCIETARIA = {"sa", "sau", "srl", "saic", "saica", "sacifa", "ciasa", "sacif", "saci", "sapem", "sh", "scpa", "ltd", "inc", "llc", "sociedad", "anonima"}
_EQUIV = {"cia": "compania", "cias": "companias", "hnos": "hermanos", "hno": "hermano", "imp": "importadora", "exp": "exportadora"}


def clave(razon: str) -> tuple:
    """Razon social normalizada para comparar por IGUALDAD: sin acentos, minusculas, abreviaturas habituales expandidas (Hnos -> Hermanos) y sin la
    forma societaria del final (S.A., S.R.L., S.A.C.I.F.I.A., Sociedad Anonima). Dos razones son «la misma» solo si la tupla es identica."""
    palabras = [_EQUIV.get(w, w) for w in re.split(r"[^a-z0-9]+", uni.normalizar(razon)) if w]
    while palabras and (palabras[-1] in _FORMA_SOCIETARIA or len(palabras[-1]) == 1):
        palabras.pop()
    return tuple(palabras)


def misma_razon(a: str, b: str) -> bool:
    """True si `a` coincide exactamente con `b` o con alguna de sus variantes (la CNV agrega a veces el alias entre parentesis)."""
    ka = clave(a)
    return bool(ka) and any(clave(v) == ka for v in uni.variantes_de_razon(b))


def leer_byma(ruta: str) -> list:
    """[{ticker, emisor, vence}] sin repetir tickers. Acepta el payload completo ({"puntos": [...]}) o una lista de filas."""
    with open(ruta, encoding="utf-8") as f:
        d = json.load(f)
    filas = d.get("puntos", []) if isinstance(d, dict) else d
    salida, vistos = [], set()
    for x in filas:
        t, e = str(x.get("ticker") or "").strip(), " ".join(str(x.get("emisor") or "").split())
        if not t or not e or t in vistos:
            continue
        vistos.add(t)
        salida.append({"ticker": t, "emisor": e, "vence": str(x.get("fecha_vencimiento") or "").strip() or None})
    return salida


def leer_ons_csv(ruta: str) -> list:
    with open(ruta, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _resolver_cuit(emisor: str, universo: list, s, buscar, consultar, dormir, pausa: float) -> dict:
    """Busca el CUIT de `emisor` en la CNV. Devuelve {"cuit", "razon_cnv"} si es seguro, o {"motivo", "candidatos"}."""
    claves = uni.palabras_clave(emisor)
    terminos = list(dict.fromkeys(t for t in [emisor.strip(), *(" ".join(claves[:n]) for n in range(len(claves), 0, -1))] if t))
    vistos, errores = {}, []
    for t in terminos:
        try:
            resultados = buscar(t, s)
        except Exception as error:
            errores.append(f"{t!r}: {type(error).__name__}: {error}"[:140])
            continue
        for x in resultados:
            cuit = re.sub(r"\D", "", str(x.get("cuit", "")))
            if len(cuit) == 11 and cuit not in vistos:
                vistos[cuit] = " ".join(str(x.get("descripcion", "")).split())
        dormir(pausa)
    candidatos = [{"cuit": c, "descripcion": d, "exacto": misma_razon(emisor, d), "cuit_valido": cuit_valido(c)} for c, d in sorted(vistos.items(), key=lambda kv: (kv[1], kv[0]))]
    if errores and not vistos:
        return {"motivo": "cnv_no_responde", "candidatos": [], "errores": errores}
    exactos = [c for c in candidatos if c["exacto"]]
    if not exactos:
        return {"motivo": "sin_coincidencia_exacta_en_autocompletado", "candidatos": candidatos[:8], "errores": errores}
    if len({c["cuit"] for c in exactos}) > 1:
        return {"motivo": "varias_coincidencias_exactas", "candidatos": exactos, "errores": errores}
    elegido = exactos[0]
    if not elegido["cuit_valido"]:
        return {"motivo": "cuit_invalido", "candidatos": exactos, "errores": errores}
    pagina = consultar(elegido["cuit"], s, dormir=dormir)
    dormir(pausa)
    if pagina["estado"] == "error":
        return {"motivo": "cnv_no_responde", "candidatos": exactos, "errores": [pagina.get("error") or "error"]}
    if pagina["estado"] != "ok" or not misma_razon(emisor, pagina["razon_social"]):
        return {"motivo": "pagina_cnv_no_confirma", "candidatos": exactos, "pagina_cnv": pagina.get("razon_social") or pagina["estado"], "errores": errores}
    return {"cuit": elegido["cuit"], "razon_cnv": pagina["razon_social"], "descripcion_autocompletado": elegido["descripcion"]}


def planificar(byma: list, universo: list, ons: list, s=None, buscar=uni.buscar_autocompletado, consultar=uni.consultar_cuit, dormir=None, pausa: float = 0.5,
               hasta_t: float | None = None) -> dict:
    """Plan (sin escribir nada): {"universo_nuevo": [...], "ons_nuevas": [...], "por_revisar": [...], "resumen": {...}}.
    `universo`: filas de data/universo.csv; `ons`: filas de data/ons.csv. Los emisores se procesan por orden alfabetico (reproducible)."""
    dormir = dormir or time.sleep
    por_clave, por_cuit = {}, {}
    for f in universo:
        if f.get("cuit"):
            por_clave.setdefault(clave(f["empresa"]), f)
            por_cuit[f["cuit"]] = f
    tickers_ons = {o["ticker"] for o in ons}
    emisores = {}
    for x in byma:
        emisores.setdefault(x["emisor"], []).append(x)
    universo_nuevo, ons_nuevas, por_revisar = [], [], []
    ya_en_universo = sin_tickers_nuevos = pendientes = 0
    nuevos_cuits = {}
    for emisor in sorted(emisores, key=lambda e: (clave(e), e)):
        filas = [x for x in emisores[emisor] if x["ticker"] not in tickers_ons]
        existente = por_clave.get(clave(emisor))
        if existente is None and not filas:
            sin_tickers_nuevos += 1
            continue
        if existente is not None:
            ya_en_universo += 1
            cuit, fuente = existente["cuit"], FUENTE_UNIVERSO
        else:
            if hasta_t is not None and time.monotonic() > hasta_t:
                pendientes += 1
                continue
            r = _resolver_cuit(emisor, universo, s, buscar, consultar, dormir, pausa)
            if "cuit" not in r:
                por_revisar.append({"emisor": emisor, "tickers": sorted(x["ticker"] for x in filas), **r})
                continue
            cuit, fuente = r["cuit"], FUENTE_CNV
            if cuit not in por_cuit and cuit not in nuevos_cuits:
                nuevos_cuits[cuit] = {"empresa": r["razon_cnv"], "cuit": cuit, "origen": ORIGEN_NUEVO}
                universo_nuevo.append(nuevos_cuits[cuit])
        for x in sorted(filas, key=lambda x: x["ticker"]):
            ons_nuevas.append({"ticker": x["ticker"], "emisor": emisor, "descripcion": "", "vence": x["vence"] or "", "cuit": cuit, "fuente_cuit": fuente})
    return {
        "universo_nuevo": universo_nuevo, "ons_nuevas": ons_nuevas,
        "por_revisar": por_revisar,
        "resumen": {"emisores_en_byma": len(emisores), "ya_en_universo": ya_en_universo, "emisores_nuevos_confirmados": len(universo_nuevo),
                    "ons_nuevas": len(ons_nuevas), "emisores_por_revisar": len(por_revisar), "emisores_sin_tickers_nuevos": sin_tickers_nuevos,
                    "pendientes_por_tiempo": pendientes},
    }


def informe_revisar(plan: dict, fuente: str) -> dict:
    """Contenido determinista de informes/emisores_por_revisar.json (sin fechas: el mismo plan da los mismos bytes)."""
    return {"version": 1, "fuente": fuente,
            "nota": "Emisores de ONs de BYMA cuyo CUIT NO se pudo confirmar con coincidencia exacta de razon social en la CNV. No estan en data/universo.csv ni en data/ons.csv: revisar uno por uno.",
            "resumen": plan["resumen"], "emisores": plan["por_revisar"]}


def escribir_informe(plan: dict, fuente: str, ruta: str = RUTA_REVISAR) -> None:
    import os

    os.makedirs(os.path.dirname(ruta) or ".", exist_ok=True)
    with open(ruta, "w", encoding="utf-8", newline="\n") as f:
        json.dump(informe_revisar(plan, fuente), f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def _agregar_filas(ruta: str, columnas: list, filas: list) -> None:
    with open(ruta, encoding="utf-8", newline="") as f:
        previo = f.read()
    with open(ruta, "a", encoding="utf-8", newline="") as f:
        if previo and not previo.endswith("\n"):
            f.write("\n")
        w = csv.DictWriter(f, fieldnames=columnas, lineterminator="\n", extrasaction="ignore")
        w.writerows(filas)


def escribir_csv(plan: dict, ruta_universo: str = "data/universo.csv", ruta_ons: str = "data/ons.csv") -> dict:
    """Agrega SOLO lo confirmado al final de los CSV (no reordena ni toca las filas existentes)."""
    if plan["universo_nuevo"]:
        _agregar_filas(ruta_universo, ["empresa", "cuit", "origen"], plan["universo_nuevo"])
    if plan["ons_nuevas"]:
        _agregar_filas(ruta_ons, COLUMNAS_ONS, plan["ons_nuevas"])
    return {"universo": len(plan["universo_nuevo"]), "ons": len(plan["ons_nuevas"])}


def a_markdown(plan: dict) -> str:
    r = plan["resumen"]
    L = ["## Emisores de ONs de BYMA", "", "| | |", "|---|--:|"] + [f"| {k.replace('_', ' ')} | {v} |" for k, v in r.items()]
    L += ["", f"### Confirmados para sumar al universo ({len(plan['universo_nuevo'])})"]
    L += [f"- {u['empresa']} (`{u['cuit']}`)" for u in plan["universo_nuevo"]] or ["- ninguno"]
    L += ["", f"### A revisar a mano ({len(plan['por_revisar'])})"]
    for e in plan["por_revisar"]:
        cands = "; ".join(f"{c['cuit']} {c['descripcion']}" for c in e.get("candidatos", [])[:3])
        L.append(f"- **{e['emisor']}** ({', '.join(e['tickers'])}): {e['motivo']}" + (f" — candidatos: {cands}" if cands else ""))
    if not plan["por_revisar"]:
        L.append("- nada")
    L += ["", "_Los CSV solo se modifican con `--escribir` y, en el workflow, a traves de un pull request que una persona debe mergear._", ""]
    return "\n".join(L)
