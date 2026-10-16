"""Fase 4: OCR opcional de PDF escaneados. Tesseract y pdf2image se reemplazan por dobles: no hace falta ningun binario."""
import gzip
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import avisos_pago as avp
from cnv import descarga, escaneados, financieros as fin, ocr, sincronizar

from .pdfs import pdf_con_paginas
from .test_tipos import Resp, SesionFalsa, pagina

EMP = {"empresa": "EMPRESA EJEMPLO S.A.", "cuit": "30111111112"}
ESCANEADO = pdf_con_paginas(["", "", ""])  # 3 paginas sin texto: una imagen escaneada


def instalar_dobles(monkeypatch, idiomas=("spa", "eng"), textos=None, fallar_pagina=None, sin_binario=False):
    """Registra pytesseract y pdf2image falsos. `textos[n]` es lo que 'lee' el OCR en la pagina n (1-indexada)."""
    llamadas = {"paginas": [], "lang": []}
    pyt = ModuleType("pytesseract")

    def get_languages(config=""):
        if sin_binario:
            raise RuntimeError("tesseract is not installed")
        return list(idiomas)

    pyt.get_languages = get_languages
    pyt.image_to_string = lambda img, lang="eng": (llamadas["lang"].append(lang), (textos or {}).get(img, f"texto reconocido por ocr en la pagina numero {img}"))[1]
    p2i = ModuleType("pdf2image")

    def convert_from_bytes(pdf, dpi=200, first_page=1, last_page=1):
        llamadas["paginas"].append(first_page)
        if fallar_pagina == first_page:
            raise RuntimeError("poppler fallo")
        return [first_page]

    p2i.convert_from_bytes = convert_from_bytes
    monkeypatch.setitem(sys.modules, "pytesseract", pyt)
    monkeypatch.setitem(sys.modules, "pdf2image", p2i)
    return llamadas


# --- configuracion -----------------------------------------------------------

@pytest.mark.parametrize("valor,esperado", [("1", True), ("true", True), ("SI", True), ("0", False), ("", False), ("no", False)])
def test_activado_solo_con_la_variable(valor, esperado):
    assert ocr.activado({"CNV_OCR": valor}) is esperado
    assert ocr.activado({}) is False


def test_configuracion_con_defectos_y_basura():
    assert ocr.configuracion({}) == {"max_paginas": 40, "idioma": "spa", "dpi": 200}
    assert ocr.configuracion({"CNV_OCR_MAX_PAGINAS": "7", "CNV_OCR_IDIOMA": "spa+eng", "CNV_OCR_DPI": "300"}) == {"max_paginas": 7, "idioma": "spa+eng", "dpi": 300}
    assert ocr.configuracion({"CNV_OCR_MAX_PAGINAS": "x", "CNV_OCR_DPI": "-1"})["max_paginas"] == 40


def test_necesita_ocr_mira_el_promedio_por_pagina():
    assert ocr.necesita_ocr("[pagina 1]\n\n[pagina 2]\n", 2)
    assert ocr.necesita_ocr("[pagina 1]\nx\n" * 3, 3)
    assert not ocr.necesita_ocr("[pagina 1]\n" + "texto util " * 20, 1)
    assert not ocr.necesita_ocr("", 0)


# --- dobles de tesseract -------------------------------------------------------

def test_comprobar_detecta_lo_que_falta(monkeypatch):
    monkeypatch.setitem(sys.modules, "pytesseract", None)  # import -> ImportError
    with pytest.raises(ocr.OcrNoDisponible, match="falta"):
        ocr.comprobar()
    instalar_dobles(monkeypatch, sin_binario=True)
    with pytest.raises(ocr.OcrNoDisponible, match="tesseract no disponible"):
        ocr.comprobar()
    instalar_dobles(monkeypatch, idiomas=("eng",))
    with pytest.raises(ocr.OcrNoDisponible, match="spa"):
        ocr.comprobar("spa+eng")
    instalar_dobles(monkeypatch)
    ocr.comprobar("spa+eng")


def test_ocr_pdf_una_pagina_a_la_vez_con_tope_y_pagina_rota(monkeypatch):
    ll = instalar_dobles(monkeypatch, fallar_pagina=2)
    r = ocr.ocr_pdf(b"%PDF", paginas_totales=10, max_paginas=3, idioma="spa", dpi=150)
    assert ll["paginas"] == [1, 2, 3] and set(ll["lang"]) == {"spa"}
    assert r["paginas_ocr"] == 3 and r["paginas_total"] == 10
    assert r["texto"] == "[pagina 1]\ntexto reconocido por ocr en la pagina numero 1\n\n[pagina 2]\n\n\n[pagina 3]\ntexto reconocido por ocr en la pagina numero 3"


# --- integracion con la descarga ----------------------------------------------

def sesion_escaneada():
    return SesionFalsa(pagina(("scan.pdf", "gs")), blobs={"gs": [Resp(200, ESCANEADO)]})


def test_sin_la_variable_el_escaneado_sigue_como_antes(monkeypatch):
    monkeypatch.delenv("CNV_OCR", raising=False)
    ll = instalar_dobles(monkeypatch)
    r = descarga.texto_de_presentacion("u", sesion_escaneada())
    assert r["escaneado"] is True and r["ocr"] == 0 and not r["ocr_intentado"] and ll["paginas"] == []


def test_con_la_variable_el_escaneado_se_lee_por_ocr(monkeypatch):
    monkeypatch.setenv("CNV_OCR", "1")
    monkeypatch.setenv("CNV_OCR_MAX_PAGINAS", "2")
    instalar_dobles(monkeypatch)
    r = descarga.texto_de_presentacion("u", sesion_escaneada())
    assert "texto reconocido por ocr en la pagina numero 1" in r["texto"] and "[pagina 3]" not in r["texto"]
    assert r["ocr"] == 2 and r["ocr_intentado"] and r["paginas"] == 3
    assert r["truncado"] == {"motivo": "ocr_paginas", "paginas_total": 3, "paginas_leidas": 2}
    assert r["escaneado"] is False


def test_el_tope_del_documento_manda_sobre_el_de_ocr(monkeypatch):
    monkeypatch.setenv("CNV_OCR", "1")
    instalar_dobles(monkeypatch)
    r = descarga.texto_de_presentacion("u", sesion_escaneada(), limites=descarga.Limites(paginas=1))
    assert r["ocr"] == 1 and r["truncado"]["motivo"] == "paginas"


def test_sin_tesseract_no_rompe_ni_marca_como_intentado(monkeypatch):
    monkeypatch.setenv("CNV_OCR", "1")
    instalar_dobles(monkeypatch, sin_binario=True)
    r = descarga.texto_de_presentacion("u", sesion_escaneada())
    assert r["escaneado"] is True and not r["ocr_intentado"] and "tesseract" in r["ocr_error"]


def test_ocr_que_no_saca_nada_queda_marcado_como_intentado(monkeypatch):
    monkeypatch.setenv("CNV_OCR", "1")
    instalar_dobles(monkeypatch, textos={1: "", 2: "", 3: ""})
    r = descarga.texto_de_presentacion("u", sesion_escaneada())
    assert r["ocr"] == 0 and r["ocr_intentado"] and r["escaneado"] is True


def test_pdf_con_texto_no_pasa_por_ocr(monkeypatch):
    monkeypatch.setenv("CNV_OCR", "1")
    ll = instalar_dobles(monkeypatch)
    s = SesionFalsa(pagina(("a.pdf", "ga")), blobs={"ga": [Resp(200, pdf_con_paginas(["Texto normal de una pagina con mas de veinticinco caracteres"]))]})
    assert descarga.texto_de_presentacion("u", s)["ocr"] == 0 and ll["paginas"] == []


# --- sincronizacion: reprocesar escaneados ----------------------------------------

def indice_con_escaneados(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, EMP["cuit"], [
        {"id": "30", "c": EMP["cuit"], "e": EMP["empresa"], "f": "2026-10-01", "t": "estados_contables", "s": "EC", "d": "EECC", "u": "https://x/publicview/30", "texto_ok": True, "escaneado": True, "intentos": 0},
        {"id": "20", "t": "hechos", "f": "2026-09-01", "s": "HR", "d": "otro", "u": "https://x/publicview/20", "texto_ok": True, "escaneado": True, "intentos": 0, "ocr_intentado": True},
        {"id": "10", "t": "hechos", "f": "2026-08-01", "texto_ok": True},
    ])
    return a


def test_reprocesar_escaneados_aplica_ocr_y_deja_rastro(tmp_path, monkeypatch):
    a = indice_con_escaneados(tmp_path)
    monkeypatch.setenv("CNV_OCR", "1")
    instalar_dobles(monkeypatch)
    monkeypatch.setattr(sincronizar, "sesion", lambda: None)
    monkeypatch.setattr(sincronizar, "texto_de_presentacion", lambda url, s=None, limites=None, hasta_t=None: descarga.texto_de_presentacion(url, sesion_escaneada(), limites=limites))
    r = sincronizar.reprocesar_escaneados(a, EMP)
    assert r == {"ocr": 1, "sin_texto": 0, "pendientes": 0}  # el 20 ya tenia ocr_intentado: no se repite
    m = {x["id"]: x for x in alm.leer_indice_empresa(a, EMP["cuit"])}["30"]
    assert m["texto_ok"] and m["ocr"] == 3 and "escaneado" not in m and "ocr_intentado" not in m and "texto reconocido por ocr en la pagina numero 1" in alm.leer_texto(a, EMP["cuit"], "30")
    sincronizar.construir_indice_global(a, [EMP])
    fila = {f["id"]: f for f in json.loads(gzip.decompress(a.leer("cnv/indice/global.json.gz")))}["30"]
    assert fila["oc"] == 3


def test_reprocesar_sin_ocr_activado_o_sin_tesseract_no_toca_nada(tmp_path, monkeypatch):
    a = indice_con_escaneados(tmp_path)
    antes = a.leer(f"cnv/indice/{EMP['cuit']}.json")
    monkeypatch.delenv("CNV_OCR", raising=False)
    assert "no esta activado" in sincronizar.reprocesar_escaneados(a, EMP)["error"]
    monkeypatch.setenv("CNV_OCR", "1")
    instalar_dobles(monkeypatch, sin_binario=True)
    assert "tesseract" in sincronizar.reprocesar_escaneados(a, EMP)["error"]
    assert a.leer(f"cnv/indice/{EMP['cuit']}.json") == antes


def test_reprocesar_marca_el_ocr_fallido_y_respeta_tope_y_tiempo(tmp_path, monkeypatch):
    a = indice_con_escaneados(tmp_path)
    monkeypatch.setenv("CNV_OCR", "1")
    instalar_dobles(monkeypatch, textos={1: "", 2: "", 3: ""})
    monkeypatch.setattr(sincronizar, "sesion", lambda: None)
    monkeypatch.setattr(sincronizar, "texto_de_presentacion", lambda url, s=None, limites=None, hasta_t=None: descarga.texto_de_presentacion(url, sesion_escaneada(), limites=limites))
    assert sincronizar.reprocesar_escaneados(a, EMP, hasta_t=1.0)["pendientes"] == 1  # tiempo vencido: no procesa
    r = sincronizar.reprocesar_escaneados(a, EMP, max_docs=5)
    assert r["sin_texto"] == 1
    m = {x["id"]: x for x in alm.leer_indice_empresa(a, EMP["cuit"])}["30"]
    assert m["ocr_intentado"] and m["escaneado"] and "ocr" not in m  # sigue marcado como escaneado, pero no se vuelve a intentar
    assert sincronizar.reprocesar_escaneados(a, EMP) == {"ocr": 0, "sin_texto": 0, "pendientes": 0}


def test_cifras_y_avisos_ignoran_documentos_leidos_por_ocr(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, EMP["cuit"], [
        {"id": "1", "t": "estados_contables", "texto_ok": True, "ocr": 5, "f": "2026-01-01"},
        {"id": "2", "t": "estados_contables", "texto_ok": True, "f": "2026-01-01"},
        {"id": "3", "t": "avisos_pago", "texto_ok": True, "ocr": 1, "f": "2026-01-01"},
        {"id": "4", "t": "avisos_pago", "texto_ok": True, "f": "2026-01-01"},
    ])
    assert [m["id"] for _, m in fin.candidatos(a, [EMP])] == ["2"]
    assert [m["id"] for _, m in avp.candidatos(a, [EMP])] == ["4"]


# --- medicion y linea de comandos ----------------------------------------------

def test_contar_escaneados_con_los_marcadores_existentes(tmp_path):
    a = indice_con_escaneados(tmp_path)
    fin.guardar_financieros(a, EMP["cuit"], {"periodos": [], "procesados": {"10": {"estado": "escaneado"}, "11": {"estado": "ok"}}})
    r = escaneados.contar(a, [EMP, {"empresa": "VACIA", "cuit": "30999999999"}])
    assert r["documentos"] == 3 and r["con_texto"] == 3 and r["marcados_escaneado"] == 2 and r["con_ocr_intentado"] == 1
    assert r["financieros_marcados_escaneado"] == 1
    assert r["por_tipo"]["hechos"] == {"documentos": 2, "escaneados": 1}
    assert r["empresas_afectadas"] == [{"empresa": EMP["empresa"], "cuit": EMP["cuit"], "escaneados": 2, "financieros_escaneado": 1}]


def test_cli_escaneados_imprime_json(tmp_path, monkeypatch, capsys):
    indice_con_escaneados(tmp_path)
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP])
    assert cli.main(["escaneados"]) == 0
    assert json.loads(capsys.readouterr().out)["marcados_escaneado"] == 2


def test_cli_ocr_sin_entorno_falla_con_mensaje(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP])
    instalar_dobles(monkeypatch, sin_binario=True)
    assert cli.main(["ocr"]) == 2
    assert "OCR no disponible" in capsys.readouterr().out


def test_cli_ocr_procesa(tmp_path, monkeypatch, capsys):
    a = indice_con_escaneados(tmp_path)
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP])
    instalar_dobles(monkeypatch)
    monkeypatch.setattr(sincronizar, "sesion", lambda: None)
    monkeypatch.setattr(sincronizar, "texto_de_presentacion", lambda url, s=None, limites=None, hasta_t=None: descarga.texto_de_presentacion(url, sesion_escaneada(), limites=limites))
    monkeypatch.setattr(cli, "reprocesar_escaneados", sincronizar.reprocesar_escaneados)
    assert cli.main(["ocr", "--solo", EMP["cuit"], "--max-docs", "1"]) == 0
    assert "1 con texto por OCR" in capsys.readouterr().out
    monkeypatch.delenv("CNV_OCR")
