"""Fase 25: series historicas por emisor. Solo puntos comparables, nada estimado, cada punto con su doc."""
import gzip
import json

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import financieros as fin
from cnv import series as ser

E = {"empresa": "EMISORA S.A.", "cuit": "30546689979"}
OTRA = {"empresa": "SIN DATOS S.A.", "cuit": "30500010912"}


def p(doc, cierre, tipo="anual", balance="consolidado", moneda="dolares", escala=1000, acumulado=True, **cifras):
    return {"doc": doc, "periodo": cierre, "tipo": tipo, "acumulado": acumulado, "balance": balance, "moneda": moneda, "unidad": f"miles de {moneda}", "escala": escala,
            "fecha_presentacion": "2026-03-01", "periodo_verificado": True, "balance_coherente": True,
            "cifras": {k: {"valor": v, "pagina": 3, "cita": f"x {v}"} for k, v in cifras.items()}}


# balance anual completo: liquidez 1,5; endeudamiento 1,5; margen 0,07; roe 0,1; deuda neta/EBITDA 2,5; cobertura cp 0,5; fcf/deuda 0,25
ANUAL = dict(activo_corriente=9_000, pasivo_corriente=6_000, pasivo_total=21_000, patrimonio_neto=14_000, activo_total=35_000, ventas=20_000, resultado_neto=1_400,
             resultado_operativo=3_000, depreciaciones_amortizaciones=1_000, intereses=-1_500, caja_equivalentes=2_000, deuda_financiera_corriente=4_000,
             deuda_financiera_no_corriente=8_000, flujo_efectivo_operativo=5_000, inversiones_capex=-2_000)


def anual(doc, anio, **kw):
    return p(str(doc), f"{anio}-12-31", **{**ANUAL, **kw})


def serie(periodos, sospechosas=None):
    return ser.serie_de_emisor(periodos, sospechosas or {})


def valores(s, tipo, ratio):
    return [(x["periodo"], x["valor"]) for x in s[tipo]["ratios"][ratio]]


def test_serie_anual_ordenada_ascendente_con_doc_balance_y_moneda_en_cada_punto():
    s = serie([anual(30, 2025), anual(10, 2023), anual(20, 2024, activo_corriente=12_000)])
    assert valores(s, "anual", "liquidez_corriente") == [("2023-12-31", 1.5), ("2024-12-31", 2.0), ("2025-12-31", 1.5)]
    punto = s["anual"]["ratios"]["liquidez_corriente"][1]
    assert punto == {"periodo": "2024-12-31", "tipo": "anual", "valor": 2.0, "balance": "consolidado", "moneda": "dolares", "doc": "20"}
    assert set(s["anual"]["ratios"]) == set(ser.RATIOS) and (s["anual"]["balance"], s["anual"]["moneda"], s["anual"]["acumulado"], s["anual"]["periodos"]) == ("consolidado", "dolares", True, 3)


def test_los_siete_ratios_pedidos_y_ninguno_mas():
    assert ser.RATIOS == ("liquidez_corriente", "endeudamiento", "margen_neto", "deuda_neta_ebitda", "cobertura_deuda_cp", "fcf_deuda", "roe")
    s = serie([anual(10, 2025)])
    assert {r: x[0]["valor"] for r, x in s["anual"]["ratios"].items()} == {"liquidez_corriente": 1.5, "endeudamiento": 1.5, "margen_neto": 0.07, "deuda_neta_ebitda": 2.5,
                                                                             "cobertura_deuda_cp": 0.5, "fcf_deuda": 0.25, "roe": 0.1}


def test_anual_y_trimestral_son_series_separadas_y_no_se_mezclan():
    trimestral = [p("40", "2026-03-31", tipo="trimestral", **ANUAL), p("41", "2026-06-30", tipo="trimestral", **{**ANUAL, "activo_corriente": 6_000})]
    s = serie([anual(10, 2025), *trimestral])
    assert set(s) == {"anual", "trimestral"}
    assert [x["doc"] for x in s["anual"]["ratios"]["liquidez_corriente"]] == ["10"] and [x["doc"] for x in s["trimestral"]["ratios"]["liquidez_corriente"]] == ["40", "41"]
    assert "roe" not in s["trimestral"]["ratios"] and "fcf_deuda" not in s["trimestral"]["ratios"]          # solo anuales: no se anualiza ni se completa


def test_valores_nulos_se_omiten_y_no_se_rellena_el_hueco():
    s = serie([anual(10, 2023), anual(20, 2024, pasivo_corriente=0), anual(30, 2025)])          # 2024: division por cero -> null
    assert valores(s, "anual", "liquidez_corriente") == [("2023-12-31", 1.5), ("2025-12-31", 1.5)]


def test_maximo_12_periodos_los_mas_recientes():
    s = serie([anual(i, 2000 + i) for i in range(1, 16)])
    pts = s["anual"]["ratios"]["liquidez_corriente"]
    assert len(pts) == 12 and pts[0]["periodo"] == "2004-12-31" and pts[-1]["periodo"] == "2015-12-31" and s["anual"]["periodos"] == 15
    assert len(s["anual"]["base"]["ventas"]["puntos"]) == 12


def test_prefiere_consolidado_y_deja_afuera_el_individual():
    s = serie([anual(10, 2024), anual(11, 2024, balance="individual", activo_corriente=1_000), anual(20, 2025), anual(21, 2025, balance="individual", activo_corriente=1_000)])
    assert [x["doc"] for x in s["anual"]["ratios"]["liquidez_corriente"]] == ["10", "20"] and s["anual"]["balance"] == "consolidado" and s["anual"]["omitidos"]["otra_pista"] == 2


def test_cambio_de_moneda_no_mezcla_pistas_y_se_queda_con_la_que_tiene_mas_ratios():
    pesos = [anual(i, 2018 + i, moneda="pesos") for i in range(1, 6)]
    dolares = [anual(i + 10, 2023 + i - 1, moneda="dolares") for i in range(1, 3)]          # solo 2 periodos en dolares
    s = serie(pesos + dolares)
    assert s["anual"]["moneda"] == "pesos" and len(s["anual"]["ratios"]["liquidez_corriente"]) == 5 and s["anual"]["omitidos"]["otra_pista"] == 2
    assert all(x["moneda"] == "pesos" for pts in s["anual"]["ratios"].values() for x in pts)


def test_con_igual_cantidad_gana_dolares_y_la_grafia_de_la_moneda_no_parte_la_serie():
    s = serie([anual(1, 2024, moneda="pesos"), anual(2, 2025, moneda="dolares")])
    assert s["anual"]["moneda"] == "dolares"
    s = serie([anual(1, 2023, moneda="Pesos Argentinos"), anual(2, 2024, moneda="pesos"), anual(3, 2025, moneda="ARS")])
    assert len(s["anual"]["ratios"]["liquidez_corriente"]) == 3 and s["anual"]["omitidos"]["otra_pista"] == 0


def test_acumulado_distinto_es_otra_pista():
    s = serie([p("1", "2026-03-31", tipo="trimestral", acumulado=True, **ANUAL), p("2", "2026-06-30", tipo="trimestral", acumulado=True, **ANUAL),
               p("3", "2026-09-30", tipo="trimestral", acumulado=False, **ANUAL)])
    assert [x["doc"] for x in s["trimestral"]["ratios"]["liquidez_corriente"]] == ["1", "2"] and s["trimestral"]["acumulado"] is True and s["trimestral"]["omitidos"]["otra_pista"] == 1


def test_cifras_base_en_la_unidad_original_con_escala_y_unidad():
    s = serie([anual(10, 2024), anual(20, 2025, ventas=25_000)])
    b = s["anual"]["base"]
    assert b["ventas"] == {"unidad": "miles de dolares", "escala": 1000, "puntos": [{"periodo": "2024-12-31", "valor": 20_000, "doc": "10", "pagina": 3},
                                                                                   {"periodo": "2025-12-31", "valor": 25_000, "doc": "20", "pagina": 3}]}
    assert set(b) == {"ventas", "resultado_neto", "patrimonio_neto", "deuda_total", "caja"} and b["patrimonio_neto"]["puntos"][0]["valor"] == 14_000
    assert b["caja"]["puntos"][0] == {"periodo": "2024-12-31", "valor": 2_000, "doc": "10", "pagina": 3, "campo": "caja_equivalentes"}


def test_deuda_total_usa_el_total_del_estado_y_si_no_la_suma_marcada_como_derivada():
    s = serie([anual(10, 2024, deuda_financiera=11_000), anual(20, 2025)])
    pts = s["anual"]["base"]["deuda_total"]["puntos"]
    assert pts[0] == {"periodo": "2024-12-31", "valor": 11_000, "doc": "10", "pagina": 3}
    assert pts[1] == {"periodo": "2025-12-31", "valor": 12_000, "doc": "20", "pagina": None, "derivado": "deuda_financiera_corriente + deuda_financiera_no_corriente"}
    sin = serie([anual(10, 2025, deuda_financiera_no_corriente=None)])
    assert "deuda_total" not in sin["anual"]["base"]


def test_caja_cae_en_efectivo_en_estados_viejos_y_no_se_inventa_si_falta():
    s = serie([anual(10, 2024, caja_equivalentes=None, efectivo=1_900), anual(20, 2025)])
    assert [(x["valor"], x["campo"]) for x in s["anual"]["base"]["caja"]["puntos"]] == [(1_900, "efectivo"), (2_000, "caja_equivalentes")]
    assert "caja" not in serie([anual(10, 2025, caja_equivalentes=None)])["anual"]["base"]


def test_cambio_de_escala_deja_solo_los_puntos_de_la_escala_mas_reciente():
    s = serie([anual(10, 2023, escala=1), anual(20, 2024, escala=1_000), anual(30, 2025, escala=1_000)])
    v = s["anual"]["base"]["ventas"]
    assert v["escala"] == 1000 and [x["doc"] for x in v["puntos"]] == ["20", "30"] and s["anual"]["omitidos"]["escala_distinta"] > 0
    assert len(s["anual"]["ratios"]["liquidez_corriente"]) == 3          # los ratios no tienen unidad: no se pierden


def test_cifras_sospechosas_no_entran_ni_en_ratios_ni_en_la_base():
    s = serie([anual(10, 2024), anual(20, 2025)], {"20": {"ventas", "caja_equivalentes"}})
    assert [x["doc"] for x in s["anual"]["base"]["ventas"]["puntos"]] == ["10"] and [x["doc"] for x in s["anual"]["base"]["caja"]["puntos"]] == ["10"]
    assert [x["doc"] for x in s["anual"]["ratios"]["margen_neto"]] == ["10"] and [x["doc"] for x in s["anual"]["ratios"]["liquidez_corriente"]] == ["10", "20"]


def test_un_tipo_sin_nada_graficable_no_aparece():
    assert serie([p("1", "2025-12-31", ventas=100)])["anual"]["base"]["ventas"]["puntos"][0]["valor"] == 100          # solo base: igual hay serie
    assert serie([p("1", "2025-12-31", tipo="otro", **ANUAL)]) == {}
    assert serie([]) == {}
    assert serie([p("1", "2025-12-31", activo_total=1)]) == {}          # ni ratios ni cifras base


def test_construir_escribe_el_indice_determinista_y_sin_reescribir(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    fin.guardar_financieros(a, E["cuit"], {"periodos": [anual(10, 2024), anual(20, 2025)], "procesados": {}})
    fin.guardar_financieros(a, OTRA["cuit"], {"periodos": [], "procesados": {}})
    a.guardar("cnv/indice/auditoria_cifras.json.gz", alm.comprimir(json.dumps({"version": 1, "sospechosas": {E["cuit"]: {"20": ["ventas"]}}}).encode()))
    r = ser.construir(a, [OTRA, E])
    assert r == {"emisores": 1, "series": 1, "puntos": 13}
    d = json.loads(gzip.decompress(a.leer(ser.RUTA)))
    assert d["version"] == 1 and list(d["emisores"]) == [E["cuit"]] and d["emisores"][E["cuit"]]["empresa"] == "EMISORA S.A."
    assert [x["doc"] for x in d["emisores"][E["cuit"]]["series"]["anual"]["base"]["ventas"]["puntos"]] == ["10"]          # la auditoria se aplica
    antes = a.leer(ser.RUTA)
    ser.construir(a, [E, OTRA])
    assert a.leer(ser.RUTA) == antes


def test_cli_series(tmp_path, monkeypatch, capsys):
    a = alm.AlmacenLocal(str(tmp_path))
    fin.guardar_financieros(a, E["cuit"], {"periodos": [anual(10, 2025)], "procesados": {}})
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [E])
    assert cli.main(["series"]) == 0 and '"emisores": 1' in capsys.readouterr().out and a.existe(ser.RUTA)
