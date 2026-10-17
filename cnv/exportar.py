"""Exportables CSV para analistas (Excel, pandas, BI): una tabla plana por tema, mas un diccionario de columnas.

Salida en cnv/export/ (todo nuevo): financieros.csv, condiciones.csv, avisos_pago.csv, hechos.csv, cobertura.csv y diccionario.md.
Formato: UTF-8 con BOM (Excel lo abre bien), separador coma, fin de linea \\n, fechas AAAA-MM-DD, campo vacio = dato no disponible (nunca 0 ni
estimado), listas unidas con ";". Cada fila lleva el `doc` de origen (y la `url` en hechos y condiciones).
Deterministas: filas ordenadas, mismos bytes si los datos no cambian, y no se reescribe un archivo igual (guardar_si_cambio).

Sin modelo y sin red. Lee del almacen lo ya verificado: financieros por empresa (con la auditoria de cifras: una cifra sospechosa sale VACIA, igual
que en los ratios) y los indices avisos_pago, condiciones y cobertura. Si un indice todavia no existe, su CSV queda con solo el encabezado.
"""
import csv
import gzip
import io
import json

from . import almacen as alm
from . import avisos_pago as avp
from . import condiciones as cond
from . import ratios as rat
from .auditoria import leer_sospechosas
from .financieros import CAMPOS as CIFRAS
from .financieros import leer_financieros

CARPETA = "cnv/export/"

_BASE = {"cuit": "CUIT del emisor (11 digitos, sin guiones)", "empresa": "razon social segun data/universo.csv", "doc": "numero de la presentacion en la CNV (clave del documento de origen)",
         "url": "URL publica de la presentacion en la CNV"}
_NUM = "numero tal cual figura en el documento, SIN convertir de unidad (ver escala); vacio = no figura o no paso la verificacion con cita"

DESCRIPCIONES = {
    "financieros.csv": {
        **{k: _BASE[k] for k in ("cuit", "empresa", "doc")},
        "periodo": "fecha de cierre del periodo", "tipo": "anual, trimestral o semestral", "acumulado": "true si las cifras de resultados son acumuladas desde el inicio del ejercicio",
        "balance": "consolidado, individual o desconocido", "moneda": "moneda en que se expresa el estado contable", "unidad": "unidad declarada (ej. miles de pesos)",
        "escala": "factor de la unidad: 1, 1000 o 1000000; valor real = cifra x escala", "fecha_presentacion": "fecha en que se presento el documento",
        "periodo_verificado": "true si la fecha de cierre figura en el texto del documento", "balance_coherente": "true si activo = pasivo + patrimonio (tolerancia 1%); vacio = no se pudo comprobar",
        "ventas": "ingresos por ventas. " + _NUM, "resultado_operativo": "resultado operativo. " + _NUM, "resultado_neto": "resultado neto del periodo. " + _NUM,
        "activo_total": "activo total. " + _NUM, "pasivo_total": "pasivo total. " + _NUM, "patrimonio_neto": "patrimonio neto. " + _NUM,
        "deuda_financiera": "deuda financiera (prestamos y obligaciones negociables). " + _NUM, "efectivo": "efectivo y equivalentes. " + _NUM,
        "activo_corriente": "activo corriente. " + _NUM, "pasivo_corriente": "pasivo corriente. " + _NUM, "intereses": "intereses (resultado financiero por intereses). " + _NUM,
        "depreciaciones_amortizaciones": "depreciaciones y amortizaciones del periodo. " + _NUM,
        "caja_equivalentes": "efectivo y equivalentes al cierre segun el estado de flujo de efectivo. " + _NUM,
        "flujo_efectivo_operativo": "flujo neto de efectivo de actividades operativas del periodo (acumulado). " + _NUM,
        "inversiones_capex": "pagos por adquisicion de propiedades, planta y equipo (con el signo con que figura). " + _NUM,
        "deuda_financiera_corriente": "prestamos y pasivos financieros corrientes. " + _NUM, "deuda_financiera_no_corriente": "prestamos y pasivos financieros no corrientes. " + _NUM,
        "resultado_por_accion": "resultado por accion basico, en moneda por accion (NO se multiplica por la escala)", "dividendos_pagados": "dividendos pagados segun el flujo de efectivo (con su signo). " + _NUM,
        "deuda_neta_ebitda": "(deuda corriente + no corriente - caja) / EBITDA; solo anuales; negativo = caja neta (veces)",
        "cobertura_deuda_cp": "caja / deuda financiera corriente (veces)", "fcf_deuda": "(flujo operativo - |capex|) / deuda corriente + no corriente; solo anuales (fraccion)",
        "cifras_sospechosas": "cifras de esta fila que la auditoria (cnv/auditoria.py) marco como inconsistentes; salen vacias aca y no entran en los ratios",
        "liquidez_corriente": "activo corriente / pasivo corriente (veces)", "endeudamiento": "pasivo total / patrimonio neto (veces)",
        "deuda_ebitda": "deuda financiera / (resultado operativo + depreciaciones); solo periodos anuales (veces)", "margen_neto": "resultado neto / ventas (fraccion, 0,05 = 5%)",
        "roe": "resultado neto / patrimonio neto; solo periodos anuales (fraccion)", "cobertura_intereses": "resultado operativo / |intereses| (veces)",
        "ratios_sin_calcular": "ratios vacios con su motivo, formato ratio:motivo separados por ;",
    },
    "condiciones.csv": {
        **_BASE, "n": "orden de la emision dentro del documento (un documento puede tener varias clases)", "tipo_documento": "prospecto, suplemento, aviso_suscripcion o aviso_resultado",
        "fecha_presentacion": "fecha en que se presento el documento", "ticker": "ticker de la ON si la emision se vinculo (CUIT + fecha de vencimiento verificada); vacio si no", "tickers": "todos los tickers vinculados, unidos con ;",
        "clase": "clase o serie tal cual figura", "moneda": "pesos, dolares, uva o dolar_linked", "tasa_tipo": "fija, variable, mixta o cupon_cero",
        "frecuencia_intereses": "mensual, trimestral, semestral, anual o al_vencimiento", "amortizacion_tipo": "bullet o cuotas",
        "monto_maximo": "monto maximo de la emision, tal cual figura", "monto_adjudicado": "monto adjudicado, tal cual figura", "tasa_valor": "tasa (porcentaje nominal anual) tal cual figura",
        "tasa_margen": "margen sobre la tasa de referencia, tal cual figura", "fecha_emision": "fecha de emision", "fecha_vencimiento": "fecha de vencimiento",
        "tasa_referencia": "tasa de referencia (ej. BADLAR)", "fechas_pago_intereses": "fechas o regla de pago de intereses, texto de la cita", "amortizacion_fechas": "fechas o regla de amortizacion, texto de la cita",
        "rescate_anticipado": "condiciones de rescate anticipado", "garantia": "garantia", "ley_aplicable": "ley aplicable", "calificacion": "calificacion de riesgo",
        "ambiguo": "campos numericos cuyo formato (coma/punto) admite dos lecturas, unidos con ;",
    },
    "avisos_pago.csv": {
        **{k: _BASE[k] for k in ("cuit", "empresa", "doc")}, "fecha": "fecha de presentacion del aviso", "emisor": "emisor tal cual figura en el aviso", "fuente": "pdf (texto del PDF) u ocr (formulario leido por OCR)",
        "serie_cnv": "serie que declara la CNV en la presentacion", "clase": "clase o serie pagada", "ticker": "ticker de la ON", "tipo_pago": "interes, amortizacion o ambos",
        "fecha_pago": "fecha de pago", "monto_total": "monto total a pagar, tal cual figura", "moneda": "pesos o dolares", "valor_residual": "valor residual, tal cual figura",
        "tasa": "tasa del servicio, tal cual figura", "valor_residual_unidad": "unidad de valor_residual (ej. %)", "tasa_unidad": "unidad de tasa", "ambiguo": "campos numericos con dos lecturas posibles, unidos con ;",
    },
    "hechos.csv": {
        "fecha": "fecha de presentacion", "cuit": _BASE["cuit"], "emisor": "razon social", "categoria": "categoria del hecho relevante (lista cerrada de la clasificacion); vacio = sin clasificar",
        "titulo": "descripcion que publica la CNV", "resumen": "resumen de una oracion generado por modelo (puede omitirse); vacio si no hay", "url": _BASE["url"], "doc": _BASE["doc"],
        "texto_ok": "true si el texto del documento esta guardado",
    },
    "cobertura.csv": {
        **{k: _BASE[k] for k in ("cuit", "empresa")}, "ons": "tickers de ONs vinculadas, unidos con ;", "cantidad_ons": "cantidad de ONs vinculadas",
        "documentos": "cantidad de documentos en el indice", "documentos_con_texto": "documentos con texto guardado", "ultimo_estado_contable_periodo": "cierre del ultimo estado contable con cifras",
        "ultimo_estado_contable_doc": "doc de ese estado contable", "ultimo_aviso_pago_fecha": "fecha de presentacion del ultimo aviso de pago",
        "ultimo_aviso_pago_estructurado": "true si ese aviso quedo con datos estructurados", "huecos": "avisos accionables (ver cnv/cobertura.py), unidos con ;",
    },
}
COLUMNAS = {
    "financieros.csv": ["cuit", "empresa", "doc", "periodo", "tipo", "acumulado", "balance", "moneda", "unidad", "escala", "fecha_presentacion", "periodo_verificado", "balance_coherente",
                        *CIFRAS, "cifras_sospechosas", *rat.ENTRADAS, "ratios_sin_calcular"],
    "condiciones.csv": ["cuit", "empresa", "doc", "n", "tipo_documento", "fecha_presentacion", "url", "ticker", "tickers", *cond.CAMPOS, "ambiguo"],
    "avisos_pago.csv": ["cuit", "empresa", "doc", "fecha", "emisor", "fuente", "serie_cnv", *[c for c in avp.CAMPOS if c != "emisor"], "valor_residual_unidad", "tasa_unidad", "ambiguo"],
    "hechos.csv": ["fecha", "cuit", "emisor", "categoria", "titulo", "resumen", "url", "doc", "texto_ok"],
    "cobertura.csv": ["cuit", "empresa", "ons", "cantidad_ons", "documentos", "documentos_con_texto", "ultimo_estado_contable_periodo", "ultimo_estado_contable_doc",
                      "ultimo_aviso_pago_fecha", "ultimo_aviso_pago_estructurado", "huecos"],
}
TITULOS = {"financieros.csv": "Estados contables: una fila por emisor y periodo", "condiciones.csv": "Condiciones de emision de ONs: una fila por emision",
           "avisos_pago.csv": "Avisos de pago de ONs: una fila por pago", "hechos.csv": "Hechos relevantes: una fila por documento",
           "cobertura.csv": "Cobertura: una fila por empresa del universo"}


def _celda(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple, set)):
        return ";".join(str(x) for x in v)
    return v


def a_csv(columnas: list, filas: list) -> bytes:
    """CSV determinista: UTF-8 con BOM, coma, \\n. `filas`: dicts; las claves que no estan en `columnas` se ignoran."""
    out = io.StringIO(newline="")
    w = csv.writer(out, lineterminator="\n")
    w.writerow(columnas)
    for f in filas:
        w.writerow([_celda(f.get(c)) for c in columnas])
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")


def _leer_gz(almacen, nombre: str):
    crudo = almacen.leer(nombre)
    if not crudo:
        return None
    try:
        return json.loads(gzip.decompress(crudo))
    except (OSError, ValueError):
        return None


def filas_financieros(almacen, empresas: list) -> list:
    auditadas = leer_sospechosas(almacen)
    filas = []
    for e in empresas:
        periodos = rat.periodos_vigentes(leer_financieros(almacen, e["cuit"])["periodos"])
        for p in periodos:
            malas = auditadas.get(e["cuit"], {}).get(p["doc"], frozenset())
            r = rat.ratios_de_periodo(p, malas)
            f = {"cuit": e["cuit"], "empresa": e["empresa"], "doc": p["doc"], "periodo": p.get("periodo"), "tipo": p.get("tipo"), "acumulado": p.get("acumulado"),
                 "balance": p.get("balance", "desconocido"), "moneda": p.get("moneda"), "unidad": p.get("unidad"), "escala": p.get("escala"),
                 "fecha_presentacion": p.get("fecha_presentacion"), "periodo_verificado": p.get("periodo_verificado"), "balance_coherente": p.get("balance_coherente")}
            f.update({c: r["insumos"][c]["valor"] if c in r["insumos"] and not r["insumos"][c].get("derivado") else None for c in CIFRAS})   # un total derivado no es una cifra del documento
            f["cifras_sospechosas"] = r.get("sospechosas")
            f.update(r["ratios"])
            f["ratios_sin_calcular"] = [f"{n}:{m}" for n, m in sorted(r["motivos"].items())]
            filas.append(f)
    filas.sort(key=lambda f: (f["empresa"], f["cuit"], f["periodo"] or "", f["balance"], f["tipo"] or "", int(f["doc"])), reverse=False)
    return filas


def filas_hechos(almacen, empresas: list) -> list:
    filas = []
    for e in empresas:
        for m in alm.leer_indice_empresa(almacen, e["cuit"]):
            if m.get("t") == "hechos":
                filas.append({"fecha": m.get("f"), "cuit": e["cuit"], "emisor": e["empresa"], "categoria": m.get("k"), "titulo": m.get("d"), "resumen": m.get("r"),
                              "url": m.get("u"), "doc": m["id"], "texto_ok": bool(m.get("texto_ok"))})
    filas.sort(key=lambda f: (f["fecha"] or "", int(f["doc"])), reverse=True)
    return filas


def filas_cobertura(almacen) -> list:
    d = _leer_gz(almacen, "cnv/indice/cobertura.json.gz") or {}
    filas = []
    for e in d.get("emisores", []):
        ec, ap = e.get("ultimo_estado_contable") or {}, e.get("ultimo_aviso_pago") or {}
        filas.append({"cuit": e["cuit"], "empresa": e["empresa"], "ons": [o["ticker"] for o in e["ons"]], "cantidad_ons": len(e["ons"]),
                      "documentos": sum(x["total"] for x in e["documentos"].values()), "documentos_con_texto": sum(x["con_texto"] for x in e["documentos"].values()),
                      "ultimo_estado_contable_periodo": ec.get("periodo"), "ultimo_estado_contable_doc": ec.get("doc"),
                      "ultimo_aviso_pago_fecha": ap.get("fecha_presentacion"), "ultimo_aviso_pago_estructurado": ap.get("estructurado") if ap else None, "huecos": e["huecos"]})
    return filas


def filas_condiciones(almacen) -> list:
    d = _leer_gz(almacen, "cnv/indice/condiciones.json.gz") or {}
    return d.get("filas", [])


def filas_avisos(almacen) -> list:
    return _leer_gz(almacen, "cnv/indice/avisos_pago.json.gz") or []


def diccionario() -> bytes:
    """diccionario.md generado desde las mismas listas de columnas que los CSV: no pueden desalinearse."""
    L = ["# Diccionario de los exportables CSV", "",
         "Generado por `python -m cnv exportar`. UTF-8 con BOM, separador coma, fechas `AAAA-MM-DD`. **Campo vacio = dato no disponible o no verificado** (nunca cero ni estimado). "
         "Listas unidas con `;`. Los numeros salen tal cual figuran en el documento (sin convertir unidades; ver `escala` en financieros).", ""]
    for nombre in COLUMNAS:
        L += [f"## {nombre}", "", TITULOS[nombre], "", "| Columna | Significado |", "|---|---|"]
        L += [f"| `{c}` | {DESCRIPCIONES[nombre][c].replace('|', '/')} |" for c in COLUMNAS[nombre]]
        L.append("")
    return "\n".join(L).encode("utf-8")


def construir(almacen, empresas: list) -> dict:
    """Escribe los CSV y el diccionario (solo los que cambiaron). Devuelve {archivo: filas} y `escritos`."""
    tablas = {"financieros.csv": filas_financieros(almacen, empresas), "condiciones.csv": filas_condiciones(almacen), "avisos_pago.csv": filas_avisos(almacen),
              "hechos.csv": filas_hechos(almacen, empresas), "cobertura.csv": filas_cobertura(almacen)}
    escritos = 0
    for nombre, filas in tablas.items():
        escritos += alm.guardar_si_cambio(almacen, CARPETA + nombre, a_csv(COLUMNAS[nombre], filas))
    escritos += alm.guardar_si_cambio(almacen, CARPETA + "diccionario.md", diccionario())
    return {**{n: len(f) for n, f in tablas.items()}, "escritos": escritos}
