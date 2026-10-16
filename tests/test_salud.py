"""Fase 5: informe de salud con indices sinteticos."""
import datetime as dt
import json

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import avisos_pago as avp
from cnv import financieros as fin
from cnv import salud

HOY = dt.date(2026, 10, 16)


def con_digito(base10: str) -> str:
    """CUIT valido a partir de sus 10 primeros digitos (calcula el verificador)."""
    resto = 11 - sum(int(d) * p for d, p in zip(base10, (5, 4, 3, 2, 7, 6, 5, 4, 3, 2))) % 11
    return base10 + str({11: 0, 10: 9}.get(resto, resto))


VIVA = {"empresa": "VIVA S.A.", "cuit": "30546689979"}        # CUIT real valido
SILENCIOSA = {"empresa": "SILENCIOSA S.A.", "cuit": con_digito("3050001091")}
DORMIDA = {"empresa": "DORMIDA S.A.", "cuit": con_digito("3070783027")}
VACIA = {"empresa": "VACIA S.A.", "cuit": con_digito("3065786305")}
MAL = {"empresa": "MAL CUIT S.A.", "cuit": con_digito("3099999999")[:-1] + "0"}


def hecho(i, f, **kw):
    return {"id": str(i), "t": "hechos", "f": f, "texto_ok": True, **kw}


@pytest.fixture
def base(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, VIVA["cuit"], [
        hecho(10, "2026-10-14", r="Resumen", k="resultados"),
        hecho(9, "2026-10-01", r="Otro", k="otro"),
        hecho(8, "2026-09-01"),
        hecho(7, "2026-08-01", escaneado=True),
        {"id": "20", "t": "estados_contables", "f": "2026-08-10", "texto_ok": True},
        {"id": "21", "t": "estados_contables", "f": "2026-05-10", "texto_ok": True},
        {"id": "22", "t": "estados_contables", "f": "2026-02-10", "texto_ok": True, "ocr": 3},  # OCR: no cuenta para cifras
        {"id": "30", "t": "prospectos", "f": "2026-05-05", "texto_ok": True, "trunc": {"motivo": "paginas"}},
        {"id": "31", "t": "avisos_pago", "f": "2026-09-30", "texto_ok": True},
        {"id": "32", "t": "avisos_pago", "f": "2026-09-20", "texto_ok": False, "intentos": 3, "error": "a.pdf: http 404"},
        {"id": "33", "t": "actas", "f": "2026-09-10", "texto_ok": False, "intentos": 1, "error": "sin adjunto"},
        {"id": "34", "t": "actas", "f": "2026-09-11", "texto_ok": False, "intentos": 0},
        {"id": "35", "t": "avisos_pago", "f": "2026-09-12", "texto_ok": True, "parcial": 1},
        {"id": "40", "t": "otros", "f": "2026-10-15", "texto_ok": False, "intentos": 0},
    ])
    fin.guardar_financieros(a, VIVA["cuit"], {"periodos": [], "procesados": {"20": {"estado": "ok"}, "21": {"estado": "escaneado"}}})
    avp.guardar_avisos(a, VIVA["cuit"], {"pagos": [], "procesados": {"31": {"estado": "ok"}}})
    alm.guardar_indice_empresa(a, SILENCIOSA["cuit"], [hecho(5, "2026-06-01"), hecho(4, "2026-03-01")])
    alm.guardar_indice_empresa(a, DORMIDA["cuit"], [hecho(3, "2024-01-01")])
    return a


UNIVERSO = [VIVA, SILENCIOSA, DORMIDA, VACIA, {"empresa": "SIN CUIT S.A.", "cuit": ""}, MAL]


def test_cuit_valido():
    assert salud.cuit_valido("30-54668997-9") and salud.cuit_valido("30546689979")
    assert not salud.cuit_valido("30546689970") and not salud.cuit_valido("123") and not salud.cuit_valido("") and not salud.cuit_valido("3054668997x")


def test_por_empresa(base):
    inf = salud.calcular(base, UNIVERSO, HOY)
    e = next(x for x in inf["empresas"] if x["cuit"] == VIVA["cuit"])
    assert e["tipos"]["hechos"] == {"total": 4, "con_texto": 4}
    assert e["tipos"]["avisos_pago"] == {"total": 3, "con_texto": 2}
    assert e["ultimo_documento"] == "2026-10-15" and e["ultimo_con_texto"] == "2026-10-14" and e["dias_sin_documentos"] == 1
    assert [(f["id"], f["rendido"], f["motivo"]) for f in e["fallidos"]] == [("33", False, "sin adjunto"), ("32", True, "a.pdf: http 404")]
    assert e["pendientes_de_bajada"] == 1  # el acta 34 nunca se intento; el tipo "otros" no se baja y no cuenta
    assert (e["escaneados"], e["truncados"], e["parciales"], e["ocr"]) == (1, 1, 1, 1)
    c = e["cobertura"]
    assert c["resumenes"] == {"hechos_con_texto": 4, "resumidos": 2, "pct": 50.0}
    assert c["categorias"]["clasificados"] == 2
    assert c["cifras"] == {"estados_con_texto": 2, "con_cifras": 1, "pct": 50.0, "estados": {"ok": 1, "escaneado": 1}}
    assert c["avisos_pago"] == {"avisos_con_texto": 2, "estructurados": 1, "pct": 50.0}


def test_silenciosas_dormidas_y_vacias(base):
    inf = salud.calcular(base, UNIVERSO, HOY)
    assert [s["empresa"] for s in inf["silenciosas"]] == ["SILENCIOSA S.A."]  # 137 dias; VIVA esta al dia; DORMIDA hace 2+ anios no es "activa"
    assert inf["silenciosas"][0]["dias"] == 137
    dormida = next(x for x in inf["empresas"] if x["cuit"] == DORMIDA["cuit"])
    assert dormida["activa"] is False and dormida["silenciosa"] is False
    assert inf["sin_documentos"] == [{"empresa": "VACIA S.A.", "cuit": VACIA["cuit"]}, {"empresa": "MAL CUIT S.A.", "cuit": MAL["cuit"]}]


def test_umbral_de_silencio_es_configurable(base):
    assert salud.calcular(base, UNIVERSO, HOY, dias_silencio=200)["silenciosas"] == []
    assert len(salud.calcular(base, UNIVERSO, dt.date(2026, 10, 16), dias_silencio=0)["silenciosas"]) == 2  # todas las activas con algun dia de atraso
    # justo en el limite: 60 dias exactos no es silenciosa, 61 si
    alm.guardar_indice_empresa(base, SILENCIOSA["cuit"], [hecho(5, (HOY - dt.timedelta(days=60)).isoformat())])
    assert salud.calcular(base, [SILENCIOSA], HOY)["silenciosas"] == []
    alm.guardar_indice_empresa(base, SILENCIOSA["cuit"], [hecho(5, (HOY - dt.timedelta(days=61)).isoformat())])
    assert len(salud.calcular(base, [SILENCIOSA], HOY)["silenciosas"]) == 1


def test_universo_sin_cuit_o_con_cuit_invalido(base):
    inf = salud.calcular(base, UNIVERSO, HOY)
    assert inf["universo_sin_cuit"] == ["SIN CUIT S.A."]
    assert inf["universo_cuit_invalido"] == [MAL]
    assert inf["resumen"]["empresas"] == 5  # solo las que tienen CUIT se consultan en el almacen


def test_totales(base):
    r = salud.calcular(base, UNIVERSO, HOY)["resumen"]
    assert r["documentos"] == 14 + 2 + 1 and r["fallidos"] == 2 and r["fallidos_rendidos"] == 1
    assert r["por_tipo"]["hechos"] == {"total": 7, "con_texto": 7}
    assert r["cobertura"]["resumenes"]["pct"] == pytest.approx(28.6, abs=0.1)
    assert r["escaneados"] == 1 and r["leidos_por_ocr"] == 1


def test_base_vacia_no_rompe(tmp_path):
    inf = salud.calcular(alm.AlmacenLocal(str(tmp_path)), [VIVA], HOY)
    assert inf["resumen"]["cobertura"]["cifras"]["pct"] is None and inf["sin_documentos"] and inf["silenciosas"] == []
    assert "ninguna" in salud.a_markdown(inf)
    assert salud.calcular(alm.AlmacenLocal(str(tmp_path)), [], HOY)["resumen"]["empresas"] == 0


def test_fechas_invalidas_se_ignoran(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, VIVA["cuit"], [hecho(1, "no-es-fecha"), hecho(2, ""), hecho(3, None), hecho(4, "2026-10-10")])
    assert salud.calcular(a, [VIVA], HOY)["empresas"][0]["ultimo_documento"] == "2026-10-10"


def test_markdown_para_el_resumen_del_job(base):
    md = salud.a_markdown(salud.calcular(base, UNIVERSO, HOY), max_fallidos=1)
    assert md.startswith("## Salud de la base CNV (2026-10-16)")
    for esperado in [f"SILENCIOSA S.A. ({SILENCIOSA['cuit']}): último documento 2026-06-01 (hace 137 días)", "VACIA S.A.", "SIN CUIT S.A.: sin CUIT",
                     "no pasa el dígito verificador", "| hechos | 7 | 7 |", "Resúmenes | 2/7", "se muestran 1", "<details>", "SILENCIOSA S.A. 🔇"]:
        assert esperado in md
    assert "RENDIDO" not in md  # max_fallidos=1: solo se muestra el primero (no rendido)
    assert "RENDIDO" in salud.a_markdown(salud.calcular(base, UNIVERSO, HOY))


def test_leer_universo_completo_incluye_filas_sin_cuit(tmp_path):
    ruta = tmp_path / "u.csv"
    ruta.write_text("empresa,cuit,origen\nA S.A.,30-54668997-9,BYMA\nB S.A.,,BYMA\n", encoding="utf-8")
    assert salud.leer_universo_completo(str(ruta)) == [{"empresa": "A S.A.", "cuit": "30546689979"}, {"empresa": "B S.A.", "cuit": ""}]


def test_universo_real_del_repo_tiene_cuit_validos():
    filas = salud.leer_universo_completo()
    assert len(filas) >= 121 and all(f["cuit"] and salud.cuit_valido(f["cuit"]) for f in filas)


def test_cli_salud_md_y_json(base, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [VIVA])
    monkeypatch.setattr(salud, "leer_universo_completo", lambda: UNIVERSO)
    assert cli.main(["salud", "--hoy", "2026-10-16"]) == 0
    assert "## Salud de la base CNV (2026-10-16)" in capsys.readouterr().out
    assert cli.main(["salud", "--formato", "json", "--hoy", "2026-10-16", "--dias-silencio", "30"]) == 0
    datos = json.loads(capsys.readouterr().out)
    assert datos["dias_silencio"] == 30 and datos["universo_sin_cuit"] == ["SIN CUIT S.A."]
