"""Estados contables escaneados de emisores con ONs: OCR con confianza + compuertas extra + periodo marcado `ocr: true`. Sin red, sin tesseract, sin API."""
import csv
import json

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import exportar, ocr, perfiles, ratios, sincronizar
from cnv import financieros as fin

from .test_financieros import BALANCE, CITAS, VALORES, ClienteFalso, respuesta

EMP = {"empresa": "EMPRESA ESCANEADA S.A.", "cuit": "30111111112"}


def indice_escaneado(tmp_path, ultima_presentacion=None):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, EMP["cuit"], [
        {"id": "30", "t": "estados_contables", "f": "2026-08-10", "u": "https://x/publicview/30", "d": "NORMA CONTABLE: NIIF - TIPO BALANCE: INDIVIDUAL", "texto_ok": True},
        {"id": "20", "t": "estados_contables", "f": "2026-05-10", "u": "https://x/publicview/20", "d": "NORMA CONTABLE: NIIF", "texto_ok": True},
        {"id": "40", "t": "estados_contables", "f": "2026-08-11", "u": "https://x/publicview/40", "d": "RELAC.: CONTROLADA - CUIT: 30-99999999-1 - OTRA SA", "texto_ok": True},
        {"id": "10", "t": "estados_contables", "f": "2026-02-10", "u": "https://x/publicview/10", "d": "NORMA CONTABLE: NIIF", "texto_ok": True},
    ])
    procesados = {i: {"v": fin.VERSION_EXTRACTOR, "estado": "escaneado"} for i in ("30", "20", "40")}
    procesados["10"] = {"v": fin.VERSION_EXTRACTOR, "estado": "ok"}
    periodos = [{"doc": "10", "periodo": "2025-12-31", "tipo": "anual", "acumulado": True, "fecha_presentacion": ultima_presentacion or "2026-02-10", "cifras": {},
                 "moneda": "pesos", "unidad": "pesos", "escala": 1, "periodo_verificado": True, "balance_coherente": True}]
    fin.guardar_financieros(a, EMP["cuit"], {"periodos": periodos, "procesados": procesados})
    return a


def doble_ocr(monkeypatch, texto_pagina="Total del activo 35.000.000", numericas=10, ilegibles=1):
    monkeypatch.setenv("CNV_OCR", "1")
    pedidos = []
    monkeypatch.setattr(ocr, "comprobar", lambda idioma="spa": None)
    monkeypatch.setattr(sincronizar, "sesion", lambda: None)
    monkeypatch.setattr(sincronizar, "pdfs_de_presentacion", lambda url, s=None, limites=None, hasta_t=None: {"pdfs": [("a.pdf", b"%PDF")], "fallos": []})
    monkeypatch.setattr(sincronizar, "extraer_pdf", lambda pdf, n=1, c=0: {"paginas": 3})

    def leer(pdf, total, maxp, idioma, dpi, minima):
        pedidos.append({"max_paginas": maxp, "dpi": dpi, "minima": minima})
        return {"texto": f"[pagina 1]\n{texto_pagina}", "paginas_ocr": 3, "paginas_total": total, "numericas": numericas, "ilegibles": ilegibles}

    monkeypatch.setattr(ocr, "ocr_pdf_confiable", leer)
    return pedidos


# --- paso de OCR ------------------------------------------------------------------------

def test_ocr_ec_lee_solo_escaneados_nuevos_propios_y_deja_el_original(tmp_path, monkeypatch):
    a = indice_escaneado(tmp_path)
    alm.guardar_texto(a, EMP["cuit"], "30", "[pagina 1]\n")
    pedidos = doble_ocr(monkeypatch)
    r = sincronizar.ocr_estados_contables(a, EMP, max_docs=5)
    assert r == {"leidos": 2, "sin_texto": 0, "pendientes": 0}       # 30 y 20: escaneados y mas nuevos que el ultimo periodo; no 40 (otra entidad) ni 10 (ya tiene cifras)
    m = {x["id"]: x for x in alm.leer_indice_empresa(a, EMP["cuit"])}
    assert m["30"]["ocr_ec"] == 3 and m["30"]["ocr_ec_numeros"] == 9 and m["30"]["ocr_ec_ilegibles"] == 1 and "ocr_ec" not in m["40"] and "ocr_ec" not in m["10"]
    assert alm.leer_texto_ocr(a, EMP["cuit"], "30").startswith("[pagina 1]") and alm.leer_texto(a, EMP["cuit"], "30") == "[pagina 1]\n"
    assert pedidos[0]["dpi"] >= 300 and pedidos[0]["max_paginas"] == sincronizar.MAX_PAGINAS_OCR_EC
    assert sincronizar.ocr_estados_contables(a, EMP)["leidos"] == 0   # idempotente


def test_ocr_ec_mas_nuevos_primero_con_tope_y_sin_numeros_confiables(tmp_path, monkeypatch):
    a = indice_escaneado(tmp_path)
    doble_ocr(monkeypatch, numericas=4, ilegibles=4)               # todo ilegible: no hay nada que extraer
    r = sincronizar.ocr_estados_contables(a, EMP, max_docs=1)
    assert r == {"leidos": 0, "sin_texto": 1, "pendientes": 0}
    m = {x["id"]: x for x in alm.leer_indice_empresa(a, EMP["cuit"])}
    assert m["30"]["ocr_ec_intentado"] and "ocr_ec" not in m["30"] and alm.leer_texto_ocr(a, EMP["cuit"], "30") is None
    assert sincronizar.ocr_estados_contables(a, EMP, max_docs=1)["sin_texto"] == 1 and {x["id"]: x for x in alm.leer_indice_empresa(a, EMP["cuit"])}["20"]["ocr_ec_intentado"]


def test_ocr_ec_sin_entorno_o_sin_tesseract_no_toca_nada(tmp_path, monkeypatch):
    a = indice_escaneado(tmp_path)
    antes = a.leer(f"cnv/indice/{EMP['cuit']}.json")
    monkeypatch.delenv("CNV_OCR", raising=False)
    assert "no esta activado" in sincronizar.ocr_estados_contables(a, EMP)["error"]
    monkeypatch.setenv("CNV_OCR", "1")

    def falla(idioma="spa"):
        raise ocr.OcrNoDisponible("falta tesseract")

    monkeypatch.setattr(ocr, "comprobar", falla)
    assert "tesseract" in sincronizar.ocr_estados_contables(a, EMP)["error"]
    assert a.leer(f"cnv/indice/{EMP['cuit']}.json") == antes


def test_ocr_ec_si_la_cnv_no_responde_no_marca_nada_y_respeta_el_tiempo(tmp_path, monkeypatch):
    a = indice_escaneado(tmp_path)
    doble_ocr(monkeypatch)

    def cae(*args, **kw):
        raise ConnectionError("sin red")

    monkeypatch.setattr(sincronizar, "pdfs_de_presentacion", cae)
    monkeypatch.setattr(sincronizar.time, "sleep", lambda s: None)
    assert sincronizar.ocr_estados_contables(a, EMP)["leidos"] == 0
    assert not any(x.get("ocr_ec_intentado") for x in alm.leer_indice_empresa(a, EMP["cuit"]))
    assert sincronizar.ocr_estados_contables(a, EMP, hasta_t=1.0)["pendientes"] == 2


def test_cli_ocr_ec_solo_emisores_con_ons(tmp_path, monkeypatch, capsys):
    a = indice_escaneado(tmp_path)
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    otra = {"empresa": "SIN ONS S.A.", "cuit": "30222222223"}
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP, otra])
    ons = tmp_path / "ons.csv"
    ons.write_text("ticker,emisor,descripcion,vence,cuit\nXX1O,Emp,desc,2030-01-01,30111111112\n", encoding="utf-8")
    doble_ocr(monkeypatch)
    assert cli.main(["ocr-ec", "--ons", str(ons), "--max-docs", "1"]) == 0
    salida = capsys.readouterr().out
    assert "1 estados contables leidos por OCR" in salida and "EMPRESA ESCANEADA" in salida and "SIN ONS" not in salida
    monkeypatch.setattr(ocr, "comprobar", lambda idioma="spa": (_ for _ in ()).throw(ocr.OcrNoDisponible("x")))
    assert cli.main(["ocr-ec", "--ons", str(ons)]) == 2
    monkeypatch.delenv("CNV_OCR", raising=False)


# --- compuertas de las cifras leidas por OCR -----------------------------------------------

def test_procesar_texto_ocr_acepta_si_el_balance_cuadra_y_marca_el_periodo():
    r = fin.procesar_texto(BALANCE, ClienteFalso(respuesta()), origen="ocr")
    assert r["estado"] == "ok" and r["ocr"] is True and r["balance_coherente"] is True and r["cifras"]["activo_total"]["valor"] == 35000000
    assert "ocr" not in fin.procesar_texto(BALANCE, ClienteFalso(respuesta()))        # del PDF con texto: sin marca


def cifras_con(**cambios):
    """respuesta() con valores cambiados; el numero viejo tambien se cambia en TODAS las citas (el OCR 'leyo' lo mismo en cada lugar)."""
    cifras = []
    for c, v in VALORES.items():
        cita = CITAS[c]
        for otro, nuevo in cambios.items():
            cita = cita.replace(f"{VALORES[otro]:,}".replace(",", "."), f"{nuevo:,}".replace(",", "."))
        cifras.append({"campo": c, "valor": cambios.get(c, v), "cita": cita})
    return cifras


def test_ocr_rechaza_balance_que_no_cuadra_aunque_la_cita_este_en_el_texto():
    # un digito mal leido en el patrimonio (14.000.000 -> 14.800.000) que ademas figura igual en el texto leido: la cita "verifica", el balance no
    texto = BALANCE.replace("14.000.000", "14.800.000")
    r = fin.procesar_texto(texto, ClienteFalso(respuesta(cifras_con(patrimonio_neto=14800000, pasivo_total=21000000))), origen="ocr")
    assert r["estado"] == "ocr_rechazado" and r["motivo"] == "balance_no_cuadra" and "cifras" not in r
    # sin OCR el mismo documento se acepta (solo informa balance_coherente=False): la compuerta es propia del OCR
    assert fin.procesar_texto(texto, ClienteFalso(respuesta(cifras_con(patrimonio_neto=14800000))))["estado"] == "ok"


def test_ocr_rechaza_balance_incompleto_periodo_y_unidad_ausentes_y_corrientes_imposibles():
    sin_pasivo = [c for c in respuesta()["cifras"] if c["campo"] != "pasivo_total"]
    assert fin.procesar_texto(BALANCE, ClienteFalso(respuesta(sin_pasivo)), origen="ocr")["motivo"] == "balance_incompleto"
    assert fin.procesar_texto(BALANCE, ClienteFalso(respuesta(unidad="millones de dolares")), origen="ocr")["motivo"] == "unidad_no_figura"
    assert fin.procesar_texto(BALANCE, ClienteFalso(respuesta(periodo="2026-03-31")), origen="ocr")["motivo"] == "periodo_no_figura"
    texto = BALANCE.replace("Total del activo corriente 9.876.543", "Total del activo corriente 40.000.000")
    r = fin.procesar_texto(texto, ClienteFalso(respuesta(cifras_con(activo_corriente=40000000))), origen="ocr")
    assert r["motivo"] == "corriente_mayor_que_total"


# --- ejecutar / candidatos / vigentes ----------------------------------------------------------

def preparar_ocr(tmp_path, texto_ocr=BALANCE):
    a = indice_escaneado(tmp_path)
    m = {x["id"]: x for x in alm.leer_indice_empresa(a, EMP["cuit"])}
    m["30"].update({"ocr_ec": 3})
    alm.guardar_indice_empresa(a, EMP["cuit"], list(m.values()))
    alm.guardar_texto(a, EMP["cuit"], "30", "[pagina 1]\n")
    alm.guardar_texto_ocr(a, EMP["cuit"], "30", texto_ocr)
    return a


def test_ejecutar_procesa_el_texto_ocr_y_guarda_el_periodo_marcado(tmp_path):
    a = preparar_ocr(tmp_path)
    assert [m["id"] for _, m in fin.candidatos(a, [EMP])] == ["30"]     # el 20 es escaneado sin OCR: no es candidato
    cliente = ClienteFalso(respuesta())
    assert fin.ejecutar(a, [EMP], cliente, max_docs=5)["ok"] == 1
    estado = fin.leer_financieros(a, EMP["cuit"])
    p = next(x for x in estado["periodos"] if x["doc"] == "30")
    assert p["ocr"] is True and estado["procesados"]["30"]["ocr"] is True
    assert fin.candidatos(a, [EMP]) == [] and len(cliente.pedidos) == 1     # no se repite
    # el indice plano lo informa
    fin.construir_indice_financieros(a, [EMP])
    filas = json.loads(__import__("gzip").decompress(a.leer("cnv/indice/financieros.json.gz")))
    assert [f.get("ocr") for f in filas if f["doc"] == "30"] == [True] and "ocr" not in next(f for f in filas if f["doc"] == "10")


def test_ejecutar_con_balance_que_no_cuadra_no_guarda_cifras_y_no_se_repite(tmp_path):
    texto = BALANCE.replace("14.000.000", "14.800.000")
    a = preparar_ocr(tmp_path, texto_ocr=texto)
    r = fin.ejecutar(a, [EMP], ClienteFalso(respuesta(cifras_con(patrimonio_neto=14800000))), max_docs=5)
    assert r["ok"] == 0 and r["saltados"] == 1
    estado = fin.leer_financieros(a, EMP["cuit"])
    assert not any(x["doc"] == "30" for x in estado["periodos"])
    assert estado["procesados"]["30"]["estado"] == "ocr_rechazado" and estado["procesados"]["30"]["motivo"] == "balance_no_cuadra"
    assert fin.candidatos(a, [EMP]) == []


def test_si_hay_texto_del_pdf_y_ocr_del_mismo_cierre_gana_el_texto():
    base = {"periodo": "2026-06-30", "tipo": "semestral", "acumulado": True, "balance": "individual", "moneda": "pesos"}
    ocr_p, texto_p = {**base, "doc": "99", "ocr": True}, {**base, "doc": "50"}
    assert fin.gana(texto_p, ocr_p) and not fin.gana(ocr_p, texto_p)
    assert fin.gana({**base, "doc": "60"}, texto_p) and not fin.gana({**base, "doc": "40"}, texto_p)
    assert ratios.periodos_vigentes([ocr_p, texto_p]) == [texto_p]
    assert ratios.periodos_vigentes([texto_p, ocr_p]) == [texto_p]


# --- los consumidores avisan "leida por OCR" -------------------------------------------------

def test_ratios_exportar_y_perfil_informan_que_las_cifras_son_de_ocr(tmp_path):
    a = preparar_ocr(tmp_path)
    fin.ejecutar(a, [EMP], ClienteFalso(respuesta()), max_docs=5)
    p = next(x for x in fin.leer_financieros(a, EMP["cuit"])["periodos"] if x["doc"] == "30")
    fila = ratios.ratios_de_periodo(p)
    assert fila["ocr"] is True and "ocr" not in ratios.ratios_de_periodo({**p, "ocr": False})
    filas = exportar.filas_financieros(a, [EMP])
    assert {f["doc"]: f["ocr"] for f in filas} == {"10": False, "30": True}
    assert "ocr" in exportar.COLUMNAS["financieros.csv"]
    frases = perfiles.frases_estado_contable({**fila, "ratios": fila["ratios"], "motivos": fila["motivos"]}, p)
    assert "leídas por OCR" in frases[0]["texto"]
    assert "leídas por OCR" not in perfiles.frases_estado_contable(ratios.ratios_de_periodo({**p, "ocr": False}), {**p, "ocr": False})[0]["texto"]
