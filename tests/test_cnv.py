import datetime as dt
import gzip
import json

import pytest

from cnv import almacen as alm
from cnv import descarga, listado, sincronizar

HTML = (
    '<h3>HECHOS RELEVANTES</h3><table class="tabla-hechos-relevantes">'
    '<tr><td>14 oct. 2026 15:27</td><td></td><td>Resultados del tercer trimestre</td><td>3580501</td><td><a href="https://x/publicview/a">v</a></td></tr>'
    '<tr><td>1 oct. 2026 10:00</td><td></td><td>Designacion de director</td><td>3570000</td><td><a href="https://x/publicview/b">v</a></td></tr></table>'
    '<h3>Estados Contables</h3><table class="tabla-hechos-relevantes">'
    '<tr><td>10 ago. 2026 18:00</td><td></td><td>Estados financieros 30/06/2026</td><td>3500000</td><td><a href="https://x/publicview/c">v</a></td></tr></table>'
    '<h3>Aviso de Pago</h3><table class="tabla-hechos-relevantes">'
    '<tr><td>2 ago. 2026 9:00</td><td></td><td>Pago de intereses</td><td>3490000</td><td><a href="https://x/publicview/d">v</a></td></tr></table>'
)


def test_parsear_listado_tipos_y_fechas():
    docs = listado.parsear_listado(HTML)
    assert [d["doc_id"] for d in docs] == ["3580501", "3570000", "3500000", "3490000"]
    assert [d["tipo"] for d in docs] == ["hechos", "hechos", "estados_contables", "otros"]
    assert docs[0]["fecha"] == "2026-10-14" and docs[2]["fecha"] == "2026-08-10"


def test_parsear_fecha_invalida_no_inventa():
    assert listado.parsear_fecha("sin fecha") == ""
    assert listado.parsear_fecha("3 xyz 2026") == ""


def test_adjuntos_de_la_pagina():
    html = '<propiedad uploader="x">[{"guid":"abc-1","nombreArchivo":"hecho.pdf"},{"guid":"","nombreArchivo":"vacio.pdf"}]</propiedad>'
    assert descarga.adjuntos(html) == [{"guid": "abc-1", "nombre": "hecho.pdf"}]


def test_pdfs_de_zip_y_pdf_directo():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.pdf", b"%PDF-1.4 x")
        z.writestr("b.txt", b"no")
    assert [n for n, _ in descarga.pdfs_de(buf.getvalue())] == ["a.pdf"]
    assert descarga.pdfs_de(b"%PDF-1.7 hola")[0][0] == "documento.pdf"
    assert descarga.pdfs_de(b"basura") == []


def test_almacen_local_ida_y_vuelta(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_texto(a, "30500000000", "123", "hola mundo")
    assert alm.leer_texto(a, "30500000000", "123") == "hola mundo"
    assert alm.leer_texto(a, "30500000000", "999") is None
    alm.guardar_indice_empresa(a, "30500000000", [{"id": "123"}])
    assert alm.leer_indice_empresa(a, "30500000000") == [{"id": "123"}]


def test_pendientes_omite_los_ya_bajados_y_los_rendidos():
    docs = listado.parsear_listado(HTML)
    indice = [{"id": "3580501", "texto_ok": True}, {"id": "3570000", "texto_ok": False, "intentos": 3}]
    assert sincronizar.pendientes(docs, indice, ("hechos",)) == []
    assert [d["doc_id"] for d in sincronizar.pendientes(docs, [], ("hechos",))] == ["3580501", "3570000"]
    assert [d["doc_id"] for d in sincronizar.pendientes(docs, [], ("hechos", "estados_contables"))][-1] == "3500000"


def test_sincronizar_empresa_baja_nuevos_y_marca_fallidos(tmp_path, monkeypatch):
    a = alm.AlmacenLocal(str(tmp_path))
    docs = listado.parsear_listado(HTML)
    monkeypatch.setattr(sincronizar, "listar", lambda cuit, desde: docs)
    monkeypatch.setattr(sincronizar, "sesion", lambda: None)
    monkeypatch.setattr(sincronizar.time, "sleep", lambda s: None)

    def falso(url, s=None):
        if url.endswith("/a"):
            return {"texto": "[pagina 1]\nResultados del trimestre", "paginas": 1, "archivos": ["a.pdf"], "escaneado": False}
        return {}  # sin adjunto

    monkeypatch.setattr(sincronizar, "texto_de_presentacion", falso)
    emp = {"empresa": "EMPRESA SA", "cuit": "30111111112"}
    r = sincronizar.sincronizar_empresa(a, emp, dt.date(2026, 1, 1), ("hechos",))
    assert r["nuevos"] == 1 and r["fallidos"] == 1
    assert alm.leer_texto(a, emp["cuit"], "3580501").startswith("[pagina 1]")
    indice = {m["id"]: m for m in alm.leer_indice_empresa(a, emp["cuit"])}
    assert indice["3580501"]["texto_ok"] and indice["3570000"]["intentos"] == 1
    assert "3490000" in indice and not indice["3490000"]["texto_ok"]  # otros tipos: solo metadatos
    # segunda corrida: no vuelve a bajar lo que ya esta
    r2 = sincronizar.sincronizar_empresa(a, emp, dt.date(2026, 1, 1), ("hechos",))
    assert r2["nuevos"] == 0


def test_indice_global_solo_con_texto(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, "30111111112", [
        {"id": "2", "c": "30111111112", "e": "X", "f": "2026-10-01", "t": "hechos", "s": "HR", "d": "uno", "u": "u", "p": 2, "n": 100, "texto_ok": True},
        {"id": "1", "c": "30111111112", "e": "X", "f": "2026-09-01", "t": "hechos", "s": "HR", "d": "dos", "u": "u", "texto_ok": False},
    ])
    n = sincronizar.construir_indice_global(a, [{"cuit": "30111111112", "empresa": "X"}])
    assert n == 1
    filas = json.loads(gzip.decompress(a.leer("cnv/indice/global.json.gz")))
    assert [f["id"] for f in filas] == ["2"]


def test_ultima_informacion_no_pisa_la_seccion_que_tipifica():
    html = (
        '<h3>Última Información Recibida</h3><table class="tabla-hechos-relevantes"><tr><td>14 oct. 2026 15:27</td><td></td>'
        '<td>- RESULTADOS</td><td>3580501</td><td><a href="https://x/publicview/a">v</a></td></tr></table>'
        '<h3>HECHOS RELEVANTES</h3><table class="tabla-hechos-relevantes"><tr><td>14 oct. 2026 15:27</td><td></td>'
        '<td>Resultados del tercer trimestre</td><td>3580501</td><td><a href="https://x/publicview/a">v</a></td></tr></table>'
    )
    docs = listado.parsear_listado(html)
    assert len(docs) == 1 and docs[0]["tipo"] == "hechos" and docs[0]["descripcion"] == "Resultados del tercer trimestre"
