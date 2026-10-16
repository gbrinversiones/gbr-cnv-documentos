"""Fase 8: avisos de pago que son un formulario cuyo PDF solo trae los rotulos. Sin OCR no se publica nada; con OCR se verifica contra ese texto."""
import json
import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import avisos_pago as avp
from cnv import descarga, ocr, sincronizar

from .pdfs import pdf_con_paginas
from .test_avisos_pago import BUENOS, Cliente, resp

AQUI = os.path.dirname(__file__)
VACIO = open(os.path.join(AQUI, "fixtures", "aviso_pago_formulario_vacio.txt"), encoding="utf-8").read()
OCR = open(os.path.join(AQUI, "fixtures", "aviso_pago_formulario_ocr.txt"), encoding="utf-8").read()
AVISO_CON_DATOS = open(os.path.join(AQUI, "fixtures", "aviso_pago_on.txt"), encoding="utf-8").read()
EMP = {"empresa": "EMPRESA EJEMPLO S.A.", "cuit": "30111111112"}

DATOS_OCR = [
    {"pago": 0, "campo": "emisor", "valor": "EMPRESA EJEMPLO S.A.", "cita": "Emisor: EMPRESA EJEMPLO S.A."},
    {"pago": 0, "campo": "clase", "valor": "Clase XXIV", "cita": "Clase / Serie: Clase XXIV"},
    {"pago": 0, "campo": "ticker", "valor": "EJC24", "cita": "Codigo de negociacion: EJC24"},
    {"pago": 0, "campo": "tipo_pago", "valor": "interes", "cita": "pago de servicios de: Intereses"},
    {"pago": 0, "campo": "fecha_pago", "valor": "2026-11-15", "cita": "Fecha de pago: 15/11/2026"},
    {"pago": 0, "campo": "monto_total", "valor": "1.875.000", "cita": "Monto total a pagar: US$ 1.875.000"},
    {"pago": 0, "campo": "moneda", "valor": "dolares", "cita": "Monto total a pagar: US$ 1.875.000"},
    {"pago": 0, "campo": "tasa", "valor": "7,5", "cita": "Tasa de interes aplicable: 7,5 % nominal anual"},
]


# --- deteccion -----------------------------------------------------------------

def test_formulario_sin_valores_se_reconoce_y_un_aviso_con_datos_no():
    assert avp.es_formulario_sin_valores(VACIO) is True
    assert avp.es_formulario_sin_valores(AVISO_CON_DATOS) is False
    assert avp.es_formulario_sin_valores("Texto cualquiera sin relacion") is False


def test_un_formulario_sin_valores_no_llama_al_modelo_ni_produce_pagos():
    c = Cliente(resp(DATOS_OCR))  # aunque el modelo "inventara" un pago completo, ni se le pregunta
    r = avp.procesar_texto(VACIO, c)
    assert r == {"estado": "formulario_sin_valores"} and c.pedidos == []


def test_el_modelo_inventando_un_pago_sobre_un_formulario_vacio_no_se_publica():
    # si por algun camino el texto vacio llegara al modelo, las citas inventadas no estan en el texto
    emisor, pagos, descartados = avp.verificar(VACIO, DATOS_OCR)
    assert pagos == [] and all(d["motivo"] in ("cita_no_esta_en_documento", "pago_sin_fecha_pago") for d in descartados)


# --- extraccion sobre el texto de OCR ---------------------------------------------------

def test_ocr_extrae_y_verifica_contra_el_texto_del_ocr():
    c = Cliente(resp(DATOS_OCR))
    r = avp.procesar_texto(OCR, c, origen="ocr")
    assert r["estado"] == "ok" and r["fuente"] == "ocr"
    p = r["pagos"][0]
    assert (p["clase"]["valor"], p["fecha_pago"]["valor"], p["monto_total"]["valor"], p["tasa"]["valor"]) == ("Clase XXIV", "2026-11-15", 1875000, 7.5)
    assert "valor_residual" not in p                                   # el OCR no lo pudo leer: no se completa
    assert "[ilegible]" in c.pedidos[0]["messages"][0]["content"] and "NUNCA completes" in c.pedidos[0]["messages"][0]["content"]


def test_ocr_cita_que_incluye_texto_ilegible_se_descarta():
    datos = DATOS_OCR + [{"pago": 0, "campo": "valor_residual", "valor": "100", "cita": "Valor residual luego del pago: [ilegible]"}]
    r = avp.procesar_texto(OCR, Cliente(resp(datos)), origen="ocr")
    assert "valor_residual" not in r["pagos"][0]
    assert {"pago": 0, "campo": "valor_residual", "motivo": "cita_con_texto_ilegible"} in r["descartados"]


def test_ocr_valor_que_el_ocr_no_leyo_no_se_acepta_aunque_el_modelo_lo_cite():
    texto = OCR.replace("US$ 1.875.000", "US$ [ilegible]").replace("15/11/2026", "[ilegible]")
    r = avp.procesar_texto(texto, Cliente(resp(DATOS_OCR)), origen="ocr")
    assert r["estado"] == "sin_valores_legibles" and "pagos" not in r  # no hay fecha respaldada: no se publica nada
    motivos = {(d["campo"], d["motivo"]) for d in r["descartados"]}
    assert ("fecha_pago", "cita_no_esta_en_documento") in motivos and ("monto_total", "cita_no_esta_en_documento") in motivos


def test_ocr_modelo_que_dice_que_no_es_un_aviso_queda_sin_valores_legibles():
    assert avp.procesar_texto(OCR, Cliente(resp([], es=False)), origen="ocr")["estado"] == "sin_valores_legibles"


# --- OCR con confianza ----------------------------------------------------------------------

def datos_tess(palabras):
    """palabras: [(texto, conf, linea)] -> dict de pytesseract.image_to_data"""
    return {"text": [p[0] for p in palabras], "conf": [p[1] for p in palabras], "block_num": [1] * len(palabras),
            "par_num": [1] * len(palabras), "line_num": [p[2] for p in palabras]}


def test_texto_con_confianza_reemplaza_numeros_dudosos_y_conserva_rotulos():
    d = datos_tess([("Fecha", 95, 1), ("de", 91, 1), ("pago:", 90, 1), ("15/11/2026", 96, 1), ("Monto", 93, 2), ("1.875.000", 40, 2), ("", -1, 2), ("rotulo", 20, 3)])
    texto, numericas, ilegibles = ocr.texto_con_confianza(d, 70)
    assert texto == "Fecha de pago: 15/11/2026\nMonto [ilegible]\nrotulo" and (numericas, ilegibles) == (2, 1)
    assert ocr.texto_con_confianza({}, 70) == ("", 0, 0)
    assert ocr.texto_con_confianza({"text": ["12"], "conf": ["x"]}, 70)[0] == "[ilegible]"  # confianza ilegible = no confiable


def test_confianza_minima_configurable():
    assert ocr.confianza_minima({}) == 70 and ocr.confianza_minima({"CNV_OCR_CONFIANZA": "85"}) == 85
    assert ocr.confianza_minima({"CNV_OCR_CONFIANZA": "500"}) == 100 and ocr.confianza_minima({"CNV_OCR_CONFIANZA": "x"}) == 70


def instalar_tesseract(monkeypatch, paginas):
    """paginas: {n: [(texto, conf, linea)]}. Devuelve la lista de dpi usados."""
    usados = []
    pyt = ModuleType("pytesseract")
    pyt.get_languages = lambda config="": ["spa", "eng"]
    pyt.Output = SimpleNamespace(DICT="dict")
    pyt.image_to_data = lambda img, lang="eng", output_type=None: datos_tess(paginas[img])
    p2i = ModuleType("pdf2image")
    p2i.convert_from_bytes = lambda pdf, dpi=200, first_page=1, last_page=1: usados.append(dpi) or [first_page]
    monkeypatch.setitem(sys.modules, "pytesseract", pyt)
    monkeypatch.setitem(sys.modules, "pdf2image", p2i)
    return usados


def test_ocr_pdf_confiable(monkeypatch):
    instalar_tesseract(monkeypatch, {1: [("Fecha:", 90, 1), ("15/11/2026", 95, 1)], 2: [("Monto:", 90, 1), ("1.875.000", 30, 1)]})
    r = ocr.ocr_pdf_confiable(b"%PDF", 2, 5, "spa", 300, 70)
    assert r["texto"] == "[pagina 1]\nFecha: 15/11/2026\n\n[pagina 2]\nMonto: [ilegible]" and (r["numericas"], r["ilegibles"], r["paginas_ocr"]) == (2, 1, 2)


def test_ocr_pdf_confiable_pagina_rota_no_tira_el_documento(monkeypatch):
    instalar_tesseract(monkeypatch, {1: [("Fecha:", 90, 1), ("15/11/2026", 95, 1)]})  # la pagina 2 no existe en el doble -> KeyError
    r = ocr.ocr_pdf_confiable(b"%PDF", 2, 5)
    assert "[pagina 2]\n" in r["texto"] and r["numericas"] == 1


# --- paso de OCR de formularios ----------------------------------------------------------------

def paginas_como_el_fixture():
    """Lo que 'lee' el OCR simulado: las palabras del fixture de OCR, todas con buena confianza, linea por linea."""
    cuerpo = OCR.split("[pagina 1]\n", 1)[1]
    palabras = [(w, 95, n) for n, linea in enumerate(cuerpo.splitlines(), 1) for w in linea.split()]
    return {1: palabras, 2: []}


def montar(tmp_path, monkeypatch, paginas_ocr):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, EMP["cuit"], [
        {"id": "30", "t": "avisos_pago", "f": "2026-10-20", "u": "https://x/publicview/30", "texto_ok": True, "serie": {"d": "Obligaciones Negociables Clase XXIV", "obs": "Aviso de Pago ON Clase XXIV"}},
        {"id": "31", "t": "avisos_pago", "f": "2026-10-21", "u": "https://x/publicview/31", "texto_ok": True},   # tiene datos: no es formulario
    ])
    alm.guardar_texto(a, EMP["cuit"], "30", VACIO)
    alm.guardar_texto(a, EMP["cuit"], "31", AVISO_CON_DATOS)
    monkeypatch.setenv("CNV_OCR", "1")
    usados = instalar_tesseract(monkeypatch, paginas_ocr)
    monkeypatch.setattr(sincronizar, "sesion", lambda: None)
    pedidos = []
    monkeypatch.setattr(sincronizar, "pdfs_de_presentacion", lambda url, s=None, limites=None, hasta_t=None: pedidos.append(url) or {"pdfs": [("a.pdf", pdf_con_paginas(["", ""]))], "fallos": []})
    return a, usados, pedidos


def indice(a):
    return {m["id"]: m for m in alm.leer_indice_empresa(a, EMP["cuit"])}


def test_ocr_formularios_guarda_el_texto_en_una_ruta_nueva_y_deja_intacto_el_original(tmp_path, monkeypatch):
    a, usados, pedidos = montar(tmp_path, monkeypatch, {1: [("Fecha:", 95, 1), ("15/11/2026", 96, 1)], 2: [("Monto", 90, 1), ("1.875.000", 91, 1)]})
    r = sincronizar.ocr_formularios(a, EMP)
    assert r == {"leidos": 1, "sin_texto": 0, "pendientes": 0} and pedidos == ["https://x/publicview/30"] and set(usados) == {300}
    assert alm.leer_texto_ocr(a, EMP["cuit"], "30") == "[pagina 1]\nFecha: 15/11/2026\n\n[pagina 2]\nMonto 1.875.000"
    assert alm.leer_texto(a, EMP["cuit"], "30") == VACIO and alm.leer_texto_ocr(a, EMP["cuit"], "31") is None
    m = indice(a)["30"]
    assert m["ocr_form"] == 2 and m["ocr_form_numeros"] == 2 and "ocr" not in m
    assert sincronizar.ocr_formularios(a, EMP)["leidos"] == 0 and len(pedidos) == 1          # idempotente


def test_ocr_formularios_sin_numeros_confiables_no_guarda_texto_y_no_reintenta(tmp_path, monkeypatch):
    a, _, pedidos = montar(tmp_path, monkeypatch, {1: [("Fecha:", 95, 1), ("15/11/2026", 20, 1)], 2: [("Monto", 90, 1), ("1.875.000", 10, 1)]})
    assert sincronizar.ocr_formularios(a, EMP) == {"leidos": 0, "sin_texto": 1, "pendientes": 0}
    assert alm.leer_texto_ocr(a, EMP["cuit"], "30") is None and indice(a)["30"]["ocr_form_intentado"] is True
    assert sincronizar.ocr_formularios(a, EMP)["sin_texto"] == 0 and len(pedidos) == 1


def test_ocr_formularios_sin_activar_o_sin_tesseract_o_con_cnv_caida(tmp_path, monkeypatch):
    a, _, _ = montar(tmp_path, monkeypatch, {})
    monkeypatch.delenv("CNV_OCR")
    assert "no esta activado" in sincronizar.ocr_formularios(a, EMP)["error"]
    monkeypatch.setenv("CNV_OCR", "1")
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    assert "falta" in sincronizar.ocr_formularios(a, EMP)["error"]
    instalar_tesseract(monkeypatch, {})
    monkeypatch.setattr(sincronizar.time, "sleep", lambda t: None)
    monkeypatch.setattr(sincronizar, "pdfs_de_presentacion", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("cae")))
    assert sincronizar.ocr_formularios(a, EMP) == {"leidos": 0, "sin_texto": 0, "pendientes": 1}
    assert "ocr_form_intentado" not in indice(a)["30"]                      # un corte de red no marca el documento como intentado
    assert sincronizar.ocr_formularios(a, EMP, hasta_t=1.0)["pendientes"] == 1


def test_de_punta_a_punta_formulario_sin_valores_luego_ocr_luego_pago(tmp_path, monkeypatch):
    a, _, _ = montar(tmp_path, monkeypatch, paginas_como_el_fixture())
    c = Cliente(resp(BUENOS))
    t = avp.ejecutar(a, [EMP], c, 10)
    assert t["formularios_sin_valores"] == 1 and t["ok"] == 1 and len(c.pedidos) == 1      # el formulario no gasto una llamada; el aviso con datos si
    assert avp.construir_indice(a, [EMP]) == 1 and json.loads(__import__("gzip").decompress(a.leer("cnv/indice/avisos_pago.json.gz")))[0]["doc"] == "31"
    # ahora llega el OCR del formulario y el documento se reevalua
    sincronizar.ocr_formularios(a, EMP)
    assert [m["id"] for _, m in avp.candidatos(a, [EMP])] == ["30"]
    t = avp.ejecutar(a, [EMP], Cliente(resp(DATOS_OCR)), 10)
    assert t["ok"] == 1
    fila = {f["doc"]: f for f in json.loads(__import__("gzip").decompress((avp.construir_indice(a, [EMP]), a.leer("cnv/indice/avisos_pago.json.gz"))[1]))}["30"]
    assert fila["fuente"] == "ocr" and fila["serie_cnv"] == "Obligaciones Negociables Clase XXIV" and fila["clase"] == "Clase XXIV"
    assert avp.candidatos(a, [EMP]) == []                                                    # y ya no se repite


def test_contar_formularios(tmp_path, monkeypatch):
    a, _, _ = montar(tmp_path, monkeypatch, {1: [("Fecha:", 95, 1), ("15/11/2026", 96, 1)], 2: [("Monto", 90, 1), ("1.875.000", 91, 1)]})
    avp.ejecutar(a, [EMP], Cliente(resp(BUENOS)), 10)
    r = avp.contar_formularios(a, [EMP])
    assert r["avisos_con_texto"] == 2 and r["formularios_sin_valores"] == 1 and r["con_texto_ocr"] == 0 and r["estados"] == {"formulario_sin_valores": 1}
    sincronizar.ocr_formularios(a, EMP)
    assert avp.contar_formularios(a, [EMP])["con_texto_ocr"] == 1


def test_cli_contar_formularios_y_ocr(tmp_path, monkeypatch, capsys):
    a, _, _ = montar(tmp_path, monkeypatch, {1: [("Fecha:", 95, 1), ("15/11/2026", 96, 1)], 2: [("Monto", 90, 1), ("1.875.000", 91, 1)]})
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP])
    assert cli.main(["avisos-pago", "--contar-formularios"]) == 0
    assert json.loads(capsys.readouterr().out)["formularios_sin_valores"] == 1
    monkeypatch.setattr(cli, "reprocesar_escaneados", lambda *a, **k: {"ocr": 0, "sin_texto": 0, "pendientes": 0})
    assert cli.main(["ocr"]) == 0
    assert "1 formularios de aviso de pago leidos por OCR" in capsys.readouterr().out


# --- metadata de serie --------------------------------------------------------------------------

@pytest.mark.parametrize("html", [
    '<propiedad DescripcionDeLaSerieIndividual="Obligaciones Negociables Clase XXIV" IDSerieIndividual="123456" Observaciones="Aviso de Pago ON Clase XXIV">',
    '<propiedad nombre="DescripcionDeLaSerieIndividual">Obligaciones Negociables Clase XXIV</propiedad><propiedad nombre="IDSerieIndividual">123456</propiedad><propiedad name="Observaciones">Aviso de Pago ON Clase XXIV</propiedad>',
    '<input name="DescripcionDeLaSerieIndividual" value="Obligaciones Negociables Clase XXIV"><input value="123456" name="IDSerieIndividual"><input name="Observaciones" value="Aviso de Pago ON Clase XXIV">',
    '{"DescripcionDeLaSerieIndividual": "Obligaciones Negociables Clase XXIV", "IDSerieIndividual": "123456", "Observaciones": "Aviso de Pago ON Clase XXIV"}',
])
def test_metadatos_de_serie_en_varias_formas(html):
    assert descarga.metadatos_serie(html) == {"d": "Obligaciones Negociables Clase XXIV", "id": "123456", "obs": "Aviso de Pago ON Clase XXIV"}


def test_metadatos_de_serie_ausentes_o_vacios():
    assert descarga.metadatos_serie("") == {} and descarga.metadatos_serie('<propiedad IDSerieIndividual="">') == {}
    assert descarga.metadatos_serie('<propiedad Observaciones="Con &amp; y <b>tags</b>   espacios">') == {"obs": "Con & y tags espacios"}


def test_la_serie_viaja_de_la_pagina_al_indice(tmp_path, monkeypatch):
    from .test_tipos import Resp, SesionFalsa

    lista = json.dumps([{"guid": "ga", "nombreArchivo": "a.pdf"}]).replace('"', "&quot;")
    html = f'<propiedad uploader="u">{lista}</propiedad><propiedad DescripcionDeLaSerieIndividual="ON Clase XXIV">'
    s = SesionFalsa(html, blobs={"ga": [Resp(200, pdf_con_paginas(["Texto de prueba suficientemente largo para la pagina"]))]})
    assert descarga.texto_de_presentacion("u", s)["serie"] == {"d": "ON Clase XXIV"}
    a = alm.AlmacenLocal(str(tmp_path))
    monkeypatch.setattr(sincronizar, "sesion", lambda: None)
    monkeypatch.setattr(sincronizar, "texto_de_presentacion", lambda *a, **k: {"texto": "[pagina 1]\nx", "paginas": 1, "archivos": ["a.pdf"], "serie": {"d": "ON Clase XXIV"}})
    d = {"doc_id": "5", "fecha": "2026-10-01", "seccion": "Aviso de Pago", "tipo": "avisos_pago", "descripcion": "x", "url": "u"}
    assert sincronizar._procesar(a, EMP, d, None)["serie"] == {"d": "ON Clase XXIV"}


def test_pdfs_de_presentacion(monkeypatch):
    from .test_tipos import Resp, SesionFalsa, pagina

    s = SesionFalsa(pagina(("a.pdf", "ga"), ("b.pdf", "gb"), ("c.pdf", "gc")), blobs={"ga": [Resp(200, pdf_con_paginas(["x"]))], "gb": [Resp(404, b"x")], "gc": [Resp(200, b"1" * 200)]})
    r = descarga.pdfs_de_presentacion("u", s, dormir=lambda t: None)
    assert [n for n, _ in r["pdfs"]] == ["a.pdf"] and {f["archivo"]: f["motivo"] for f in r["fallos"]} == {"b.pdf": "http 404", "c.pdf": "no es PDF ni ZIP con PDF"}
