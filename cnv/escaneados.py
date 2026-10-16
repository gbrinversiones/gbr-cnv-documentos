"""Cuantos documentos del universo son imagenes escaneadas, segun los marcadores que ya deja el sistema.

Marcadores:
  - indice por empresa: `escaneado` = True cuando el texto guardado es (casi) solo las marcas de pagina (< 25 caracteres por pagina);
    `ocr_intentado` / `ocr` los deja el paso OCR. Los documentos bajados antes de esta marca no la tienen: para ellos vale el
    marcador de financieros (solo estados contables)
  - cnv/financieros/{cuit}.json: procesados[*].estado == "escaneado" (el texto existe pero casi no tiene cifras)
Solo lee; no escribe nada.
"""
from . import almacen as alm
from .financieros import leer_financieros


def contar(almacen, empresas: list) -> dict:
    r = {"documentos": 0, "con_texto": 0, "marcados_escaneado": 0, "con_ocr_intentado": 0, "leidos_por_ocr": 0,
         "financieros_marcados_escaneado": 0, "por_tipo": {}, "empresas_afectadas": []}
    for e in empresas:
        propios = 0
        for m in alm.leer_indice_empresa(almacen, e["cuit"]):
            t = r["por_tipo"].setdefault(m.get("t") or "?", {"documentos": 0, "escaneados": 0})
            r["documentos"] += 1
            t["documentos"] += 1
            r["con_texto"] += bool(m.get("texto_ok"))
            if m.get("ocr"):
                r["leidos_por_ocr"] += 1
            if m.get("escaneado"):
                r["marcados_escaneado"] += 1
                r["con_ocr_intentado"] += bool(m.get("ocr_intentado"))
                t["escaneados"] += 1
                propios += 1
        marcados = sum(1 for p in leer_financieros(almacen, e["cuit"])["procesados"].values() if p.get("estado") == "escaneado")
        r["financieros_marcados_escaneado"] += marcados
        if propios or marcados:
            r["empresas_afectadas"].append({"empresa": e["empresa"], "cuit": e["cuit"], "escaneados": propios, "financieros_escaneado": marcados})
    r["empresas_afectadas"].sort(key=lambda x: -(x["escaneados"] + x["financieros_escaneado"]))
    return r
