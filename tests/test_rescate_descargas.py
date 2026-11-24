from cnv import almacen as alm
from cnv import reintentos


def _indice(tmp_path):
    almacen = alm.AlmacenLocal(str(tmp_path))
    docs = [
        {"id": "1", "t": "hechos", "texto_ok": False, "intentos": 3, "error": "ReadTimeout"},
        {"id": "2", "t": "hechos", "texto_ok": False, "intentos": 3, "error": "sin texto", "ocr_intentado": True},
        {"id": "3", "t": "hechos", "texto_ok": False, "intentos": 3, "error": "sin adjunto"},
        {"id": "4", "t": "hechos", "texto_ok": False, "intentos": 3, "error": "503", "rescate": 1},
        {"id": "5", "t": "hechos", "texto_ok": True, "intentos": 0},
        {"id": "6", "t": "hechos", "texto_ok": False, "intentos": 1, "error": "ReadTimeout"},
    ]
    alm.guardar_indice_empresa(almacen, "30111111111", docs)
    return almacen


def test_rescata_solo_lo_que_fallo_por_la_red_y_una_sola_vez(tmp_path):
    almacen = _indice(tmp_path)
    empresas = [{"empresa": "X", "cuit": "30111111111"}]
    assert reintentos.rescatar_descargas(almacen, empresas) == {"rescatados": 1, "empresas": 1}
    docs = {m["id"]: m for m in alm.leer_indice_empresa(almacen, "30111111111")}
    assert docs["1"]["intentos"] == 0 and docs["1"]["rescate"] == 1
    assert all(docs[i]["intentos"] == 3 for i in ("2", "3", "4"))
    # segunda corrida: ya no hay nada que rescatar (un rescate por documento)
    docs["1"]["intentos"] = 3
    alm.guardar_indice_empresa(almacen, "30111111111", list(docs.values()))
    assert reintentos.rescatar_descargas(almacen, empresas)["rescatados"] == 0


def test_prioridad_limita_a_esas_empresas(tmp_path):
    almacen = _indice(tmp_path)
    assert reintentos.rescatar_descargas(almacen, [{"empresa": "X", "cuit": "30111111111"}], prioridad={"30999999999"})["rescatados"] == 0
