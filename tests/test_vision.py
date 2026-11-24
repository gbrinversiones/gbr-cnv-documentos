import io

from pypdf import PdfWriter

from cnv import almacen as alm
from cnv import concurrencia as conc
from cnv import vision


def _pdf(paginas: int) -> bytes:
    w = PdfWriter()
    for _ in range(paginas):
        w.add_blank_page(width=200, height=200)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


class _Bloque:
    type = "text"

    def __init__(self, texto):
        self.text = texto


class _Resp:
    def __init__(self, texto):
        self.content = [_Bloque(texto)]
        self.usage = None
        self.stop_reason = "end_turn"


class _Cliente:
    def __init__(self, texto):
        self.llamadas = []
        self.texto = texto
        self.messages = self

    def create(self, **kw):
        self.llamadas.append(kw)
        return _Resp(self.texto)


def test_partir_pdf_en_tandas_y_con_tope():
    partes = vision.partir_pdf(_pdf(25), max_paginas=22, por_parte=10)
    assert [p for p, _ in partes] == [1, 11, 21]


def test_candidatos_solo_sin_texto_que_ya_pasaron_por_ocr():
    indice = [
        {"id": "1", "t": "hechos", "texto_ok": False, "u": "x", "ocr_intentado": True, "f": "2026-01-01"},
        {"id": "2", "t": "hechos", "texto_ok": True, "u": "x", "escaneado": True},
        {"id": "3", "t": "hechos", "texto_ok": False, "u": "x", "ocr_intentado": True, "vision_intentado": True},
        {"id": "4", "t": "hechos", "texto_ok": False, "u": "x", "error": "ReadTimeout"},
        {"id": "5", "t": "prospectos", "texto_ok": False, "u": "x", "error": "sin texto", "f": "2026-05-01"},
    ]
    assert [m["id"] for m in vision.candidatos(indice, ("hechos", "prospectos"))] == ["5", "1"]


def test_ejecutar_guarda_texto_y_marca(tmp_path, monkeypatch):
    monkeypatch.setattr(conc, "presupuesto", lambda: type("P", (), {"esperar": lambda self, t: None})())
    almacen = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(almacen, "30111111111", [
        {"id": "7", "t": "hechos", "texto_ok": False, "u": "u7", "ocr_intentado": True, "error": "sin texto"},
        {"id": "8", "t": "hechos", "texto_ok": False, "u": "u8", "ocr_intentado": True},
    ])
    cliente = _Cliente("[pagina 1]\n" + "Hecho relevante: la sociedad informa el pago de la ON Clase 5 por 1.234.567. " * 10)
    pdfs = {"u7": [("a.pdf", _pdf(3))], "u8": []}
    r = vision.ejecutar(almacen, [{"empresa": "X", "cuit": "30111111111"}], cliente, ("hechos",), 10, 1e18, lambda u: pdfs[u])
    assert r == {"leidos": 1, "sin_texto": 1, "fallidos": 0, "pendientes": 0}
    docs = {m["id"]: m for m in alm.leer_indice_empresa(almacen, "30111111111")}
    assert docs["7"]["texto_ok"] and docs["7"]["vision"] and docs["7"]["ocr"] == 3 and "error" not in docs["7"]
    assert docs["8"]["vision_intentado"] and not docs["8"].get("texto_ok")
    assert "Clase 5" in alm.leer_texto(almacen, "30111111111", "7")
    assert cliente.llamadas[0]["messages"][0]["content"][0]["type"] == "document"
