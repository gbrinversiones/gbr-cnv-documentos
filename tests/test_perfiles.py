"""Fase 22: perfil de emisor determinista (texto plantilla, sin modelo). Fixtures sinteticos."""
import datetime as dt
import gzip
import json
import re

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import financieros as fin
from cnv import perfiles as per

HOY = dt.date(2026, 10, 16)
A = {"empresa": "EMISORA A S.A.", "cuit": "30546689979"}
B = {"empresa": "SIN DATOS S.A.", "cuit": "30500010912"}
ONS = [{"ticker": "AA1O", "emisor": "A", "descripcion": "", "vence": "2035-06-18", "cuit": A["cuit"]}, {"ticker": "AA2O", "emisor": "A", "descripcion": "", "vence": "2028-01-01", "cuit": A["cuit"]}]
PALABRAS_VALORATIVAS = ("bueno", "buena", "malo", "mala", "excelente", "sólido", "solido", "débil", "debil", "preocupante", "saludable", "riesgoso", "fuerte", "alto", "alta", "bajo", "baja",
                        "robusto", "favorable", "desfavorable", "positivo", "negativo", "recomend", "atractiv", "problema")


def cifra(v, pagina=1):
    return {"valor": v, "pagina": pagina, "cita": f"x {v}"}


def periodo(doc, cierre="2026-06-30", tipo="semestral", balance="consolidado", moneda="dolares", **cifras):
    return {"doc": doc, "periodo": cierre, "tipo": tipo, "acumulado": True, "balance": balance, "moneda": moneda, "unidad": "miles de dolares" if moneda == "dolares" else "miles de pesos", "escala": 1000,
            "fecha_presentacion": "2026-08-10", "periodo_verificado": True, "balance_coherente": True, "cifras": {k: cifra(v) for k, v in cifras.items()}}


CONSOLIDADO = dict(activo_corriente=9_000, pasivo_corriente=6_000, pasivo_total=21_000, patrimonio_neto=14_000, activo_total=35_000, ventas=20_000, resultado_neto=1_400,
                   resultado_operativo=3_000, depreciaciones_amortizaciones=1_000, intereses=-1_500)


@pytest.fixture
def base(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    fin.guardar_financieros(a, A["cuit"], {"periodos": [periodo("100", **CONSOLIDADO), periodo("101", balance="individual", moneda="pesos", pasivo_total=21_000, patrimonio_neto=14_000)], "procesados": {}})
    alm.guardar_indice_empresa(a, A["cuit"], [
        {"id": "200", "t": "hechos", "f": "2026-10-10", "texto_ok": True, "k": "dividendos"}, {"id": "201", "t": "hechos", "f": "2026-10-12", "texto_ok": True, "k": "emision_on"},
        {"id": "202", "t": "hechos", "f": "2026-09-01", "texto_ok": True}, {"id": "203", "t": "hechos", "f": "2026-08-01", "texto_ok": True, "k": "otro"},
        {"id": "204", "t": "estados_contables", "f": "2026-10-13", "texto_ok": True}, {"id": "205", "t": "prospectos", "f": "2026-05-01", "texto_ok": True}])
    cond = {"version": 1, "filas": [], "ons": {"AA1O": {"cuit": A["cuit"], "vence": "2035-06-18", "vinculo": "cuit+fecha_vencimiento",
                                                         "campos": {"fecha_vencimiento": "2035-06-18", "tasa_tipo": "fija", "tasa_valor": 7.55},
                                                         "fuentes": {"fecha_vencimiento": {"doc": "300", "tipo_documento": "suplemento", "pagina": 2}, "tasa_tipo": {"doc": "300"}, "tasa_valor": {"doc": "301"}},
                                                         "conflictos": [], "documentos": ["300", "301"], "emisiones": [["300", 0]]}}}
    a.guardar("cnv/indice/condiciones.json.gz", alm.comprimir(json.dumps(cond).encode()))
    return a


def perfil_de(a, e=A, ons=ONS, universo=None):
    per.construir(a, universo or [A, B], ons, HOY)
    return json.loads(gzip.decompress(a.leer("cnv/indice/perfiles.json.gz")))["perfiles"][e["cuit"]]


def test_formato_de_numeros_y_fechas():
    assert per.num(1234567.891) == "1.234.567,89" and per.num(1.5) == "1,50" and per.num(1234567, 0) == "1.234.567" and per.num(-0.07, 1) == "-0,1" and per.num(7.55) == "7,55"
    assert per.fecha("2026-06-30") == "30/06/2026" and per.fecha(None) == "None" and per.fecha("x") == "x"


def test_perfil_completo_texto_exacto(base):
    p = perfil_de(base)
    tipos = [f["tipo"] for f in p["frases"]]
    assert tipos[:3] == ["empresa", "estado_contable", "cifras"] and tipos.count("ratio") == 4 and "on_condiciones" in tipos and tipos.count("hecho_relevante") == 3
    textos = {t: [f["texto"] for f in p["frases"] if f["tipo"] == t] for t in set(tipos)}
    assert textos["empresa"] == ["EMISORA A S.A., CUIT 30546689979."]
    assert textos["estado_contable"] == ["Su último estado contable con cifras verificadas es del período semestral cerrado el 30/06/2026, balance consolidado, en dólares (documento 100 de la CNV)."]
    assert textos["cifras"] == ["Cifras tal como figuran en el documento, expresadas en miles de dolares: ventas 20.000, resultado neto 1.400, activo total 35.000, pasivo total 21.000, patrimonio neto 14.000."]
    assert "Liquidez corriente: 1,50 veces (activo corriente dividido pasivo corriente)." in textos["ratio"]
    assert "Margen neto: 7,0 % (resultado neto dividido ventas)." in textos["ratio"] and "Endeudamiento: 1,50 veces (pasivo total dividido patrimonio neto)." in textos["ratio"]
    assert "Cobertura de intereses: 2,00 veces (resultado operativo dividido intereses)." in textos["ratio"]
    assert textos["ratios_sin_calcular"][0].startswith("No se pudieron calcular: caja sobre deuda financiera corriente (falta una cifra en el estado contable)")
    assert "rentabilidad sobre patrimonio (solo se calcula con períodos anuales)" in textos["ratios_sin_calcular"][0]
    assert textos["ons"] == ["En el catálogo de obligaciones negociables figuran 2: AA1O (vence 18/06/2035), AA2O (vence 01/01/2028)."]
    assert textos["on_condiciones"] == ["Según su documento de emisión, la obligación negociable AA1O vence el 18/06/2035, tasa fija de 7,55 %."]
    assert textos["hecho_relevante"] == ["El 12/10/2026 presentó un hecho relevante (categoría: emisión de obligaciones negociables).",
                                        "El 10/10/2026 presentó un hecho relevante (categoría: dividendos).", "El 01/09/2026 presentó un hecho relevante (categoría: sin categoría asignada)."]
    assert p["texto"] == " ".join(f["texto"] for f in p["frases"])


def test_cada_frase_trae_su_documento_o_su_fuente(base):
    for cuit in (A, B):
        for f in perfil_de(base, cuit)["frases"]:
            assert f["docs"] or f["fuente"], f
    p = perfil_de(base)
    por_tipo = {f["tipo"]: f for f in p["frases"]}
    assert por_tipo["estado_contable"]["docs"] == ["100"] and por_tipo["ratio"]["docs"] == ["100"] and por_tipo["on_condiciones"]["docs"] == ["300", "301"]
    assert por_tipo["ons"]["fuente"] == "data/ons.csv" and por_tipo["empresa"]["fuente"] == "data/universo.csv" and por_tipo["huecos"]["fuente"] == "cnv/indice/cobertura.json.gz"
    assert [f["docs"] for f in p["frases"] if f["tipo"] == "hecho_relevante"] == [["201"], ["200"], ["202"]]


def test_se_usa_la_fila_de_la_ficha_el_consolidado_y_no_el_individual(base):
    p = perfil_de(base)
    assert "balance consolidado, en dólares (documento 100" in p["frases"][1]["texto"] and not any("101" in f["docs"] for f in p["frases"])


def test_solo_individual_lo_dice(base):
    fin.guardar_financieros(base, A["cuit"], {"periodos": [periodo("101", balance="individual", moneda="pesos", **CONSOLIDADO)], "procesados": {}})
    assert "balance individual, en pesos (documento 101" in perfil_de(base)["frases"][1]["texto"]


def test_emisor_sin_datos_no_inventa_nada(base):
    p = perfil_de(base, B)
    assert [f["tipo"] for f in p["frases"]] == ["empresa", "estado_contable", "huecos"]
    assert p["frases"][1]["texto"] == "No hay estados contables con cifras verificadas en la base."
    assert "no hay ningun documento de la empresa en el indice" in p["frases"][2]["texto"]


def test_ratio_sin_calcular_por_cifra_sospechosa_y_sin_linea_de_intereses(base):
    sin_intereses = {k: v for k, v in CONSOLIDADO.items() if k != "intereses"}
    fin.guardar_financieros(base, A["cuit"], {"periodos": [periodo("100", **sin_intereses)], "procesados": {}})
    t = perfil_de(base)["texto"]
    assert "cobertura de intereses (el estado contable no trae una línea de intereses)" in t
    base.guardar("cnv/indice/auditoria_cifras.json.gz", alm.comprimir(json.dumps({"version": 1, "sospechosas": {A["cuit"]: {"100": ["ventas"]}}}).encode()))
    t = perfil_de(base)["texto"]
    assert "margen neto (una cifra no pasó la auditoría contable)" in t and "Margen neto:" not in t


def test_ratios_anuales_y_porcentajes(base):
    anual = dict(CONSOLIDADO, deuda_financiera=12_000)
    fin.guardar_financieros(base, A["cuit"], {"periodos": [periodo("100", cierre="2025-12-31", tipo="anual", **anual)], "procesados": {}})
    t = perfil_de(base)["texto"]
    assert "Deuda financiera sobre EBITDA: 3,00 veces" in t and "Rentabilidad sobre patrimonio: 10,0 %" in t and "período anual cerrado el 31/12/2025" in t


def test_sin_adjetivos_valorativos_en_ningun_perfil(base):
    for e in (A, B):
        t = perfil_de(base, e)["texto"].lower()
        for palabra in PALABRAS_VALORATIVAS:
            assert not re.search(r"\b" + palabra, t), (palabra, t)


def test_determinista_y_sin_reescribir(base):
    per.construir(base, [A, B], ONS, HOY)
    antes = base.leer("cnv/indice/perfiles.json.gz")
    r = per.construir(base, [B, A], ONS, HOY)          # el orden del universo no cambia el resultado
    assert r["perfiles"] == 2 and r["sin_estado_contable"] == 1 and r["frases"] == sum(len(p["frases"]) for p in json.loads(gzip.decompress(base.leer("cnv/indice/perfiles.json.gz")))["perfiles"].values())
    assert base.leer("cnv/indice/perfiles.json.gz") == antes


def test_condiciones_ilegibles_o_ausentes_no_rompen(base):
    base.guardar("cnv/indice/condiciones.json.gz", b"no es gzip")
    assert not any(f["tipo"] == "on_condiciones" for f in perfil_de(base)["frases"])
    base.guardar("cnv/indice/condiciones.json.gz", alm.comprimir(b"[]"))
    assert not any(f["tipo"] == "on_condiciones" for f in perfil_de(base)["frases"])


def test_on_con_condiciones_parciales(base):
    cond = {"version": 1, "filas": [], "ons": {"AA2O": {"campos": {"tasa_tipo": "variable"}, "fuentes": {"tasa_tipo": {"doc": "310"}}}, "AA1O": {"campos": {}, "fuentes": {}}}}
    base.guardar("cnv/indice/condiciones.json.gz", alm.comprimir(json.dumps(cond).encode()))
    f = [f for f in perfil_de(base)["frases"] if f["tipo"] == "on_condiciones"]
    assert [x["texto"] for x in f] == ["Según su documento de emisión, la obligación negociable AA2O tasa variable."] and f[0]["docs"] == ["310"]


def test_cli_perfiles(base, monkeypatch, capsys, tmp_path):
    ruta = tmp_path / "ons.csv"
    ruta.write_text("ticker,emisor,descripcion,vence,cuit,fuente_cuit\nAA1O,A,,2035-06-18," + A["cuit"] + ",x\n", encoding="utf-8")
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [A, B])
    assert cli.main(["perfiles", "--ons", str(ruta), "--hoy", "2026-10-16"]) == 0
    assert '"perfiles": 2' in capsys.readouterr().out
