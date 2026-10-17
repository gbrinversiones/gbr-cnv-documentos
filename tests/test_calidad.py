"""Fase 19: muestra de calidad y normalizacion de moneda en la auditoria. Sin modelo; indices sinteticos."""
import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import auditoria as aud
from cnv import calidad as cal
from cnv import financieros as fin

from .test_ratios import COMPLETO, periodo

E1 = {"empresa": "UNO S.A.", "cuit": "30546689979"}
E2 = {"empresa": "DOS S.A.", "cuit": "30500010912"}


@pytest.mark.parametrize("escrito,esperado", [
    ("pesos", "pesos"), ("Pesos Argentinos", "pesos"), (" PESOS ", "pesos"), ("ARS", "pesos"), ("$", "pesos"), ("pesos argentinos", "pesos"),
    ("Dólares Estadounidenses", "dolares"), ("US$", "dolares"), ("USD", "dolares"), ("U$S", "dolares"), ("dolares", "dolares"),
    ("euros", "euros"), ("", ""), (None, "")])
def test_moneda_canonica(escrito, esperado):
    assert aud.moneda_canonica(escrito) == esperado


def test_distinta_escritura_de_la_misma_moneda_no_es_un_cambio_de_moneda_ni_parte_la_serie():
    ps = [periodo(doc="10", periodo_="2023-12-31", moneda="pesos", activo_total=100), periodo(doc="11", periodo_="2024-12-31", moneda="Pesos Argentinos", activo_total=120),
          periodo(doc="12", periodo_="2025-12-31", moneda="pesos", activo_total=9_000_000)]
    hs = aud.auditar_serie(ps)
    assert not any(h["regla"] == "moneda_cambia" for h in hs)
    assert any(h["regla"] == "salto_entre_periodos" for h in hs)          # el salto se ve porque la serie ya no se parte por la escritura
    real = aud.auditar_serie([periodo(doc="10", periodo_="2023-12-31", moneda="pesos"), periodo(doc="11", periodo_="2024-12-31", moneda="Dólares")])
    assert [h["regla"] for h in real] == ["moneda_cambia"]


def completo(doc, cierre, **kw):
    return periodo(doc=doc, periodo_=cierre, **{**COMPLETO, "activo_total": 35_000, **kw})


def test_periodo_sano():
    r = cal.revisar_periodo(completo("1", "2025-12-31"), None)
    assert r["veredicto"] == "ok" and r["checks"]["balance"]["ok"] is True and r["checks"]["unidad"]["ok"] is None and r["checks"]["respaldo"]["ok"] is True


def test_balance_que_no_cierra_es_sospechoso():
    r = cal.revisar_periodo(completo("1", "2025-12-31", activo_total=50_000), None)
    assert r["veredicto"] == "sospechoso" and r["checks"]["balance"]["ok"] is False and "balance_no_cuadra" in r["checks"]["auditoria"]["detalle"]


def test_ratio_fuera_de_rango_es_revisar_no_error():
    r = cal.revisar_periodo(completo("1", "2025-12-31", activo_corriente=30_000, pasivo_corriente=1_000, deuda_financiera_corriente=500, deuda_financiera_no_corriente=11_500), None)       # liquidez 30: raro pero no imposible
    assert r["veredicto"] == "revisar" and r["checks"]["ratios"]["ok"] is False and "liquidez_corriente=30 fuera de [0.05, 20]" in r["checks"]["ratios"]["detalle"]


def test_cambio_de_escala_o_moneda_contra_el_periodo_anterior():
    ant = completo("1", "2024-12-31")
    assert cal.revisar_periodo(completo("2", "2025-12-31"), ant)["checks"]["unidad"]["ok"] is True
    assert cal.revisar_periodo(completo("2", "2025-12-31", ), {**ant, "moneda": "Pesos Argentinos"})["checks"]["unidad"]["ok"] is True
    r = cal.revisar_periodo(periodo(doc="2", periodo_="2025-12-31", escala=1_000_000, **COMPLETO), ant)
    assert r["checks"]["unidad"]["ok"] is False and r["veredicto"] == "revisar" and "escala 1000 -> 1000000" in r["checks"]["unidad"]["detalle"]
    assert cal.revisar_periodo(periodo(doc="2", periodo_="2025-12-31", moneda="dolares", **COMPLETO), ant)["checks"]["unidad"]["ok"] is False


def test_cifra_sin_cita_o_pagina_no_tiene_respaldo():
    p = completo("1", "2025-12-31")
    p["cifras"]["ventas"].pop("cita")
    p["cifras"]["intereses"]["pagina"] = None
    r = cal.revisar_periodo(p, None)
    assert r["checks"]["respaldo"] == {"ok": False, "detalle": "sin cita o pagina: intereses, ventas"} and r["veredicto"] == "sospechoso"


def test_lo_que_no_se_puede_comprobar_queda_en_none_y_no_cuenta_como_error():
    r = cal.revisar_periodo(periodo(doc="1", periodo_="2025-12-31", ventas=100), None)
    assert r["checks"]["balance"]["ok"] is None and r["checks"]["ratios"]["ok"] is None and r["veredicto"] == "ok"


def test_la_muestra_es_determinista_y_respeta_el_tamano(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    for e, base in ((E1, 10), (E2, 100)):
        fin.guardar_financieros(a, e["cuit"], {"periodos": [completo(str(base + i), f"{2015 + i}-12-31") for i in range(10)], "procesados": {}})
    todos = cal.recolectar(a, [E1, E2])
    assert len(todos) == 20 and todos[0][2] is None and todos[1][2]["periodo"] == "2015-12-31"
    m1, m2 = cal.muestrear(todos, 7), cal.muestrear(list(reversed(todos)), 7)
    assert len(m1) == 7 and [(x[0]["cuit"], x[1]["doc"]) for x in m1] == [(x[0]["cuit"], x[1]["doc"]) for x in m2]
    inf = cal.calcular(a, [E1, E2], 30)
    assert inf["muestra"] == 20 and inf["periodos_en_base"] == 20 and sum(inf["veredictos"].values()) == 20


def test_informe_markdown_y_cli(tmp_path, monkeypatch, capsys):
    a = alm.AlmacenLocal(str(tmp_path))
    fin.guardar_financieros(a, E1["cuit"], {"periodos": [completo("1", "2024-12-31"), completo("2", "2025-12-31", activo_total=50_000)], "procesados": {}})
    inf = cal.calcular(a, [E1], 30)
    md = cal.a_markdown(inf)
    assert "## Muestra de calidad de cifras (2 de 2 periodos)" in md and "balance_no_cuadra | 1" in md and "### Detalle de lo que no está ok (1)" in md and "UNO S.A. #2" in md
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [E1])
    assert cli.main(["calidad", "--n", "5"]) == 0
    assert "Muestra de calidad" in capsys.readouterr().out
    salida = tmp_path / "calidad.md"
    assert cli.main(["calidad", "--salida", str(salida)]) == 0 and salida.read_text(encoding="utf-8").startswith("## Muestra de calidad")


def test_base_vacia_no_rompe(tmp_path):
    inf = cal.calcular(alm.AlmacenLocal(str(tmp_path)), [E1])
    assert inf["muestra"] == 0 and "sin periodos" in cal.a_markdown(inf)
