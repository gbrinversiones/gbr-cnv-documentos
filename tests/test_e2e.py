"""Fase 7: prueba de punta a punta con un almacen local y fixtures:
sync -> indice -> financieros -> resumenes -> clasificar -> avisos-pago -> salud, y una segunda pasada que no repite nada."""
import gzip
import json
import os

import pytest
from types import SimpleNamespace

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import descarga, listado, salud, sincronizar

from .falsos import AnthropicFalso, CnvFalsa
from .pdfs import pdf_con_paginas
from .test_avisos_pago import BUENOS
from .test_financieros import BALANCE, CITAS, VALORES, respuesta

AQUI = os.path.dirname(__file__)
LISTADO = open(os.path.join(AQUI, "fixtures", "listado_completo.html"), encoding="utf-8").read()
AVISO = open(os.path.join(AQUI, "fixtures", "aviso_pago_on.txt"), encoding="utf-8").read()
EMP = {"empresa": "EMPRESA EJEMPLO S.A.", "cuit": "30111111112"}
HECHO = "La Sociedad informa que su Directorio aprobo la recompra de obligaciones negociables Clase 5 por hasta US$ 5.000.000 en el mercado."


def paginas_de(texto: str) -> list:
    """Texto con marcas [pagina N] -> lista de textos por pagina (para armar un PDF de verdad)."""
    return [t.split("]\n", 1)[1].strip() if "]\n" in t else "" for t in texto.split("[pagina ")[1:]]


@pytest.fixture
def mundo(tmp_path, monkeypatch):
    pdfs = {
        "h1": pdf_con_paginas([HECHO]),
        "e1": pdf_con_paginas(paginas_de(BALANCE)),
        "p1": pdf_con_paginas([f"Prospecto programa global pagina {i} " + "condiciones de emision " * 5 for i in range(1, 9)]),
        "a1": pdf_con_paginas(paginas_de(AVISO)),
    }
    cnv = CnvFalsa(LISTADO, pdfs, defecto=pdf_con_paginas(["Documento de ejemplo con texto suficiente para quedar guardado en la base."]))
    for modulo in (listado, descarga, sincronizar):
        monkeypatch.setattr(modulo, "sesion", lambda: cnv)
    monkeypatch.setattr(sincronizar.time, "sleep", lambda t: None)
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "de-prueba")
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP])
    monkeypatch.setattr(salud, "leer_universo_completo", lambda: [EMP, {"empresa": "SIN CUIT SA", "cuit": ""}])
    claude = AnthropicFalso({
        "financieros": respuesta(),
        "resumenes": "La empresa aprobo recomprar ONs Clase 5 por hasta US$ 5 millones.",
        "clasificacion": {"categoria": "recompra_on", "cita": "aprobo la recompra de obligaciones negociables Clase 5"},
        "avisos_pago": {"es_aviso_pago_on": True, "datos": BUENOS},
    })
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: claude)
    return SimpleNamespace(cnv=cnv, claude=claude, almacen=alm.AlmacenLocal(str(tmp_path)))


def pipeline(capsys=None):
    codigos = [
        cli.main(["sync", "--desde", "2026-01-01", "--max-paginas", "6"]),
        cli.main(["indice"]),
        cli.main(["financieros", "--max", "10"]),
        cli.main(["resumenes", "--max", "10"]),
        cli.main(["clasificar", "--max", "10"]),
        cli.main(["avisos-pago", "--max", "10"]),
    ]
    return codigos


def leer_gz(a, nombre):
    return json.loads(gzip.decompress(a.leer(nombre)))


def test_pipeline_completo(mundo, capsys):
    a = mundo.almacen
    assert pipeline() == [0] * 6

    # sync: los 5 tipos con texto, "otros" solo como metadato
    indice = {m["id"]: m for m in alm.leer_indice_empresa(a, EMP["cuit"])}
    assert sum(1 for m in indice.values() if m["texto_ok"]) == 9 and indice["3570000"]["texto_ok"] is False
    assert indice["3400100"]["trunc"] == {"motivo": "paginas", "paginas_total": 8, "paginas_leidas": 6}  # --max-paginas 6 (el balance tiene 6 y entra entero)

    # indice global: k, r, tr
    glob = {f["id"]: f for f in leer_gz(a, "cnv/indice/global.json.gz")}
    assert len(glob) == 9
    assert glob["3580501"]["k"] == "recompra_on" and glob["3580501"]["r"].startswith("La empresa aprobo") and glob["3400100"]["tr"] == "paginas"
    assert glob["3500000"]["k"] is None

    # financieros: una fila con las 8 cifras
    filas = leer_gz(a, "cnv/indice/financieros.json.gz")
    assert len(filas) == 1 and filas[0]["periodo"] == "2026-06-30" and filas[0]["ventas"] == VALORES["ventas"] and filas[0]["balance"] == "consolidado"
    assert filas[0]["periodo_verificado"] is True

    # avisos de pago: una fila
    pagos = leer_gz(a, "cnv/indice/avisos_pago.json.gz")
    assert len(pagos) == 1 and pagos[0]["clase"] == "Clase 12" and pagos[0]["fecha_pago"] == "2026-11-15" and pagos[0]["monto_total"] == 1875000

    # salud sobre lo que dejo el pipeline
    assert cli.main(["salud", "--formato", "json", "--hoy", "2026-10-16"]) == 0
    salida = capsys.readouterr().out
    informe = json.loads(salida[salida.index('{\n "fecha"'):])
    r = informe["resumen"]
    assert r["documentos"] == 10 and r["fallidos"] == 0 and r["truncados"] == 1
    assert r["cobertura"]["resumenes"]["pct"] == 100.0 and r["cobertura"]["cifras"]["pct"] == 100.0 and r["cobertura"]["avisos_pago"]["pct"] == 100.0
    assert informe["universo_sin_cuit"] == ["SIN CUIT SA"] and informe["silenciosas"] == []


def test_segunda_pasada_no_repite_descargas_ni_gasta_modelo(mundo):
    pipeline()
    descargas, llamadas = len(mundo.cnv.bajadas), dict(mundo.claude.llamadas)
    assert descargas == 9 and llamadas == {"financieros": 1, "resumenes": 1, "clasificacion": 1, "avisos_pago": 1}
    antes = {n: mundo.almacen.leer(n) for n in ("cnv/indice/financieros.json.gz", "cnv/indice/avisos_pago.json.gz")}
    assert pipeline() == [0] * 6
    assert len(mundo.cnv.bajadas) == descargas  # idempotente: nada se baja de nuevo
    assert mundo.claude.llamadas == llamadas     # ...ni se vuelve a pagar
    assert {n: mundo.almacen.leer(n) for n in antes} == antes


def test_un_corte_de_la_cnv_se_recupera_en_la_proxima_corrida(mundo):
    mundo.cnv.fallar["a1"] = 99  # el aviso de pago no se puede bajar (503 siempre)
    assert cli.main(["sync", "--desde", "2026-01-01"]) == 0
    m = {x["id"]: x for x in alm.leer_indice_empresa(mundo.almacen, EMP["cuit"])}["3490000"]
    assert m["texto_ok"] is False and m["intentos"] == 1 and "503" in m["error"]
    assert sum(1 for x in alm.leer_indice_empresa(mundo.almacen, EMP["cuit"]) if x["texto_ok"]) == 8  # lo demas quedo guardado
    mundo.cnv.fallar["a1"] = 0  # vuelve la CNV
    assert cli.main(["sync", "--desde", "2026-01-01"]) == 0
    m = {x["id"]: x for x in alm.leer_indice_empresa(mundo.almacen, EMP["cuit"])}["3490000"]
    assert m["texto_ok"] is True and "error" not in m
