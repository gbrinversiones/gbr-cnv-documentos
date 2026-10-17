"""Fase 10: ratios por emisor. Solo con cifras verificadas, del mismo periodo/moneda/unidad; null con motivo y nunca una estimacion."""
import gzip
import json

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import financieros as fin
from cnv import ratios as rat

from .test_financieros import BALANCE, ClienteFalso, respuesta

EMP = {"empresa": "EMPRESA EJEMPLO S.A.", "cuit": "30111111112"}


def periodo(doc="10", periodo_="2025-12-31", tipo="anual", acumulado=True, moneda="pesos", escala=1000, balance="consolidado", **cifras):
    """Entrada de periodos[] de financieros, con las cifras dadas (campo=valor)."""
    return {"doc": doc, "empresa": EMP["empresa"], "periodo": periodo_, "tipo": tipo, "acumulado": acumulado, "moneda": moneda, "unidad": "miles de pesos",
            "escala": escala, "balance": balance, "periodo_verificado": True, "balance_coherente": True,
            "cifras": {c: {"valor": v, "valor_abs": v * escala, "cita": f"{c} {v}", "pagina": 3} for c, v in cifras.items()}}


COMPLETO = dict(activo_corriente=9_000, pasivo_corriente=6_000, pasivo_total=21_000, patrimonio_neto=14_000, deuda_financiera=12_000,
                resultado_operativo=3_000, depreciaciones_amortizaciones=1_000, resultado_neto=1_400, ventas=20_000, intereses=-1_500,
                caja_equivalentes=2_000, deuda_financiera_corriente=4_000, deuda_financiera_no_corriente=8_000, flujo_efectivo_operativo=5_000, inversiones_capex=-2_000)


# --- casos felices -----------------------------------------------------------------

def test_todos_los_ratios_de_un_balance_anual_completo():
    f = rat.ratios_de_periodo(periodo(**COMPLETO))
    assert f["ratios"] == {"liquidez_corriente": 1.5, "endeudamiento": 1.5, "deuda_ebitda": 3.0, "margen_neto": 0.07, "roe": 0.1, "cobertura_intereses": 2.0,
                          "deuda_neta_ebitda": 2.5, "cobertura_deuda_cp": 0.5, "fcf_deuda": 0.25}
    assert f["motivos"] == {} and f["derivados"] == {"ebitda": 4000, "deuda_total": 12000, "deuda_neta": 10000, "fcf": 3000}
    assert f["insumos"]["pasivo_corriente"] == {"valor": 6000, "pagina": 3} and f["doc"] == "10" and f["balance"] == "consolidado"
    assert set(f["insumos"]) == {c for e in rat.ENTRADAS.values() for c in e}


def test_el_resultado_se_redondea_a_6_decimales():
    f = rat.ratios_de_periodo(periodo(**{**COMPLETO, "activo_corriente": 1, "pasivo_corriente": 3}))
    assert f["ratios"]["liquidez_corriente"] == 0.333333


def test_ebitda_suma_las_depreciaciones_en_valor_absoluto_y_cobertura_usa_intereses_absolutos():
    a = rat.ratios_de_periodo(periodo(**{**COMPLETO, "depreciaciones_amortizaciones": -1_000}))   # impresa entre parentesis
    assert a["derivados"]["ebitda"] == 4000 and a["ratios"]["deuda_ebitda"] == 3.0
    b = rat.ratios_de_periodo(periodo(**{**COMPLETO, "intereses": 1_500}))
    assert b["ratios"]["cobertura_intereses"] == 2.0


def test_la_escala_no_cambia_los_cocientes():
    miles = rat.ratios_de_periodo(periodo(escala=1000, **COMPLETO))["ratios"]
    millones = rat.ratios_de_periodo(periodo(escala=1_000_000, **COMPLETO))["ratios"]
    assert miles == millones


# --- faltantes: null con motivo, nunca estimar -----------------------------------------

def test_cada_cifra_faltante_deja_null_solo_los_ratios_que_la_usan():
    sin = dict(COMPLETO)
    del sin["pasivo_corriente"]
    f = rat.ratios_de_periodo(periodo(**sin))
    assert f["ratios"]["liquidez_corriente"] is None and f["motivos"]["liquidez_corriente"] == "falta_cifra:pasivo_corriente"
    assert f["ratios"]["endeudamiento"] == 1.5 and "pasivo_corriente" not in f["insumos"]


def test_sin_ninguna_cifra_todo_es_null_con_motivo():
    f = rat.ratios_de_periodo(periodo())
    assert all(v is None for v in f["ratios"].values()) and set(f["motivos"]) == set(rat.ENTRADAS) and f["insumos"] == {}
    assert f["motivos"]["deuda_ebitda"] == "falta_cifra:deuda_financiera,resultado_operativo,depreciaciones_amortizaciones"


def test_un_balance_viejo_sin_los_campos_nuevos_no_inventa_liquidez_ni_ebitda():
    viejo = {k: v for k, v in COMPLETO.items() if k not in ("activo_corriente", "pasivo_corriente", "intereses", "depreciaciones_amortizaciones")}
    f = rat.ratios_de_periodo(periodo(**viejo))
    assert f["ratios"]["liquidez_corriente"] is None and f["ratios"]["deuda_ebitda"] is None and f["ratios"]["cobertura_intereses"] is None
    assert f["ratios"]["endeudamiento"] == 1.5 and f["ratios"]["margen_neto"] == 0.07 and f["ratios"]["roe"] == 0.1


def test_una_cifra_que_no_es_numero_se_trata_como_faltante():
    p = periodo(**COMPLETO)
    p["cifras"]["ventas"]["valor"] = "20.000"
    p["cifras"]["resultado_neto"]["valor"] = True
    assert rat.ratios_de_periodo(p)["motivos"]["margen_neto"] == "falta_cifra:resultado_neto,ventas"


# --- contexto: periodos, monedas y unidades distintas ---------------------------------------------

def ins(valor, **ctx):
    base = {"valor": valor, "pagina": 1, "periodo": "2025-12-31", "tipo": "anual", "acumulado": True, "moneda": "pesos", "escala": 1000, "doc": "10", "balance": "consolidado"}
    return {**base, **ctx}


@pytest.mark.parametrize("ctx,motivo", [
    ({"periodo": "2025-06-30"}, "periodos_distintos"),
    ({"tipo": "semestral"}, "periodos_distintos"),
    ({"acumulado": False}, "periodos_distintos"),
    ({"moneda": "dolares"}, "monedas_distintas"),
    ({"moneda": ""}, "moneda_desconocida"),
    ({"escala": 1_000_000}, "unidades_distintas"),
    ({"escala": 7}, "unidad_desconocida"),
    ({"escala": None}, "unidad_desconocida"),
    ({"balance": "individual"}, "balances_distintos"),
])
def test_insumos_de_contextos_distintos_no_se_combinan(ctx, motivo):
    insumos = {"pasivo_total": ins(100), "patrimonio_neto": ins(50, **ctx)}
    assert rat.calcular_ratio("endeudamiento", insumos) == (None, motivo, {})


def test_mismos_contextos_si_se_combinan_aunque_vengan_de_otro_documento():
    insumos = {"pasivo_total": ins(100, doc="1"), "patrimonio_neto": ins(50, doc="2")}
    assert rat.calcular_ratio("endeudamiento", insumos) == (2.0, None, {})


def test_contexto_comun_de_un_solo_insumo_y_moneda_en_mayusculas():
    assert rat.contexto_comun([ins(1)]) is None
    p = periodo(moneda=" PESOS ", **COMPLETO)
    assert rat.contexto(p, "ventas")["moneda"] == "pesos" and rat.contexto(p, "inexistente") is None


def test_moneda_sin_declarar_deja_todo_null():
    f = rat.ratios_de_periodo(periodo(moneda="", **COMPLETO))
    assert all(v is None for v in f["ratios"].values()) and set(f["motivos"].values()) == {"moneda_desconocida"}


# --- division por cero y signos ---------------------------------------------------------------------

@pytest.mark.parametrize("cambio,ratio,motivo", [
    ({"pasivo_corriente": 0}, "liquidez_corriente", "division_por_cero"),
    ({"pasivo_corriente": -5}, "liquidez_corriente", "pasivo_corriente_negativo"),
    ({"activo_corriente": -1}, "liquidez_corriente", "activo_corriente_negativo"),
    ({"patrimonio_neto": 0}, "endeudamiento", "division_por_cero"),
    ({"patrimonio_neto": -2_000}, "endeudamiento", "patrimonio_negativo"),
    ({"patrimonio_neto": -2_000}, "roe", "patrimonio_negativo"),
    ({"patrimonio_neto": 0}, "roe", "division_por_cero"),
    ({"pasivo_total": -1}, "endeudamiento", "pasivo_total_negativo"),
    ({"ventas": 0}, "margen_neto", "division_por_cero"),
    ({"ventas": -10}, "margen_neto", "ventas_negativo"),
    ({"intereses": 0}, "cobertura_intereses", "division_por_cero"),
    ({"resultado_operativo": -1_000, "depreciaciones_amortizaciones": 1_000}, "deuda_ebitda", "division_por_cero"),
    ({"resultado_operativo": -3_000}, "deuda_ebitda", "ebitda_negativo"),
    ({"deuda_financiera": -1}, "deuda_ebitda", "deuda_financiera_negativa"),
])
def test_denominadores_y_signos_sin_sentido_dejan_null(cambio, ratio, motivo):
    f = rat.ratios_de_periodo(periodo(**{**COMPLETO, **cambio}))
    assert f["ratios"][ratio] is None and f["motivos"][ratio] == motivo


def test_patrimonio_negativo_no_contamina_los_demas_ratios():
    f = rat.ratios_de_periodo(periodo(**{**COMPLETO, "patrimonio_neto": -2_000}))
    assert f["ratios"]["liquidez_corriente"] == 1.5 and f["ratios"]["margen_neto"] == 0.07 and f["ratios"]["cobertura_intereses"] == 2.0


def test_perdidas_dan_ratios_negativos_validos():
    f = rat.ratios_de_periodo(periodo(**{**COMPLETO, "resultado_neto": -2_800, "resultado_operativo": -500}))
    assert f["ratios"]["margen_neto"] == -0.14 and f["ratios"]["roe"] == -0.2 and f["ratios"]["cobertura_intereses"] == pytest.approx(-0.333333)


# --- periodos parciales: no se anualiza ----------------------------------------------------------------

@pytest.mark.parametrize("tipo,acumulado", [("semestral", True), ("trimestral", True), ("trimestral", False), ("anual", False)])
def test_roe_y_deuda_ebitda_solo_en_periodos_anuales(tipo, acumulado):
    f = rat.ratios_de_periodo(periodo(tipo=tipo, acumulado=acumulado, **COMPLETO))
    assert f["motivos"]["roe"] == f["motivos"]["deuda_ebitda"] == "periodo_no_anual"
    # los ratios que no mezclan flujo con saldo si se calculan en cualquier periodo
    assert f["ratios"]["margen_neto"] == 0.07 and f["ratios"]["liquidez_corriente"] == 1.5 and f["ratios"]["cobertura_intereses"] == 2.0


# --- indice ---------------------------------------------------------------------------------------------

def guardar(a, periodos, cuit=EMP["cuit"]):
    fin.guardar_financieros(a, cuit, {"periodos": periodos, "procesados": {}})


def test_indice_por_emisor_con_trazabilidad_orden_y_dedupe(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    guardar(a, [periodo(doc="10", **COMPLETO), periodo(doc="12", **{**COMPLETO, "ventas": 10_000}),                      # mismo periodo: gana el doc 12
                periodo(doc="11", periodo_="2024-12-31", **COMPLETO), periodo(doc="13", balance="individual", **COMPLETO)])
    guardar(a, [], cuit="30222222223")
    stats = rat.construir(a, [EMP, {"empresa": "SIN DATOS", "cuit": "30222222223"}, {"empresa": "NO EXISTE", "cuit": "30333333334"}])
    salida = json.loads(gzip.decompress(a.leer("cnv/indice/ratios.json.gz")))
    assert salida["version"] == 1 and set(salida["formulas"]) == set(rat.ENTRADAS) and list(salida["emisores"]) == [EMP["cuit"]]
    filas = salida["emisores"][EMP["cuit"]]["periodos"]
    assert [(f["periodo"], f["balance"], f["doc"]) for f in filas] == [("2025-12-31", "individual", "13"), ("2025-12-31", "consolidado", "12"), ("2024-12-31", "consolidado", "11")]
    assert filas[1]["ratios"]["margen_neto"] == 0.14 and filas[1]["insumos"]["ventas"] == {"valor": 10_000, "pagina": 3}
    assert stats["emisores"] == 1 and stats["periodos"] == 3 and stats["calculados"]["roe"] == 3 and stats["motivos"] == {}


def test_estadisticas_cuentan_los_motivos_sin_el_detalle(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    guardar(a, [periodo(doc="10", **{k: v for k, v in COMPLETO.items() if k != "ventas"}), periodo(doc="11", tipo="semestral", periodo_="2025-06-30", **COMPLETO)])
    s = rat.construir(a, [EMP])
    assert s["motivos"] == {"falta_cifra": 1, "periodo_no_anual": 4} and s["calculados"]["liquidez_corriente"] == 2


def test_idempotente_mismos_bytes(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    guardar(a, [periodo(**COMPLETO)])
    rat.construir(a, [EMP])
    antes = a.leer("cnv/indice/ratios.json.gz")
    rat.construir(a, [EMP])
    assert a.leer("cnv/indice/ratios.json.gz") == antes


def test_de_punta_a_punta_desde_un_balance_de_texto(tmp_path):
    """Texto -> extractor (doble) -> financieros -> ratios: los 4 campos nuevos salen citados del documento."""
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, EMP["cuit"], [{"id": "7", "t": "estados_contables", "f": "2026-08-10", "texto_ok": True, "d": "TIPO BALANCE: CONSOLIDADO"}])
    alm.guardar_texto(a, EMP["cuit"], "7", BALANCE)
    fin.ejecutar(a, [EMP], ClienteFalso(respuesta()), max_docs=5)
    rat.construir(a, [EMP])
    f = json.loads(gzip.decompress(a.leer("cnv/indice/ratios.json.gz")))["emisores"][EMP["cuit"]]["periodos"][0]
    assert f["doc"] == "7" and f["tipo"] == "semestral"
    assert f["ratios"]["liquidez_corriente"] == round(9_876_543 / 8_400_000, 6) and f["ratios"]["endeudamiento"] == 1.5 and f["ratios"]["margen_neto"] == round(1_250_000 / 18_400_250, 6)
    assert f["ratios"]["cobertura_intereses"] == round(3_300_250 / 1_100_000, 6)
    assert f["motivos"] == {"deuda_ebitda": "periodo_no_anual", "roe": "periodo_no_anual", "deuda_neta_ebitda": "periodo_no_anual", "fcf_deuda": "periodo_no_anual"}      # semestral: no se anualiza


def test_cli_ratios(tmp_path, monkeypatch, capsys):
    a = alm.AlmacenLocal(str(tmp_path))
    guardar(a, [periodo(**COMPLETO)])
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP])
    assert cli.main(["ratios"]) == 0
    assert "'periodos': 1" in capsys.readouterr().out and a.existe("cnv/indice/ratios.json.gz")


# --- Fase 16: flujo y deuda ---------------------------------------------------------

def ratios16(**cambios):
    return rat.ratios_de_periodo(periodo(**{**COMPLETO, **cambios}))


def test_ratios_de_flujo_y_deuda_con_valores_a_mano():
    f = ratios16()
    # deuda total 4.000 + 8.000 = 12.000; caja 2.000; EBITDA 3.000 + 1.000 = 4.000; flujo 5.000 - |capex| 2.000 = 3.000
    assert f["ratios"]["deuda_neta_ebitda"] == 2.5 and f["ratios"]["cobertura_deuda_cp"] == 0.5 and f["ratios"]["fcf_deuda"] == 0.25
    assert f["derivados"]["deuda_neta"] == 10_000 and f["derivados"]["fcf"] == 3_000


def test_capex_cuenta_igual_con_signo_positivo_o_negativo():
    assert ratios16(inversiones_capex=2_000)["ratios"]["fcf_deuda"] == ratios16(inversiones_capex=-2_000)["ratios"]["fcf_deuda"] == 0.25


def test_caja_neta_da_deuda_neta_negativa_y_fcf_negativo_es_valido():
    assert ratios16(caja_equivalentes=20_000)["ratios"]["deuda_neta_ebitda"] == -2.0
    assert ratios16(flujo_efectivo_operativo=1_000)["ratios"]["fcf_deuda"] == round(-1_000 / 12_000, 6)


def test_semestral_solo_calcula_lo_que_son_dos_saldos():
    f = rat.ratios_de_periodo(periodo(tipo="semestral", **COMPLETO))
    assert f["ratios"]["cobertura_deuda_cp"] == 0.5
    assert f["motivos"]["deuda_neta_ebitda"] == f["motivos"]["fcf_deuda"] == "periodo_no_anual"


@pytest.mark.parametrize("cambios,ratio,motivo", [
    ({"caja_equivalentes": None}, "deuda_neta_ebitda", "falta_cifra:caja_equivalentes"),
    ({"deuda_financiera_no_corriente": None}, "fcf_deuda", "falta_cifra:deuda_financiera_no_corriente"),
    ({"deuda_financiera_corriente": None}, "cobertura_deuda_cp", "falta_cifra:deuda_financiera_corriente"),
    ({"deuda_financiera_corriente": 0}, "cobertura_deuda_cp", "division_por_cero"),
    ({"deuda_financiera_corriente": 0, "deuda_financiera_no_corriente": 0}, "fcf_deuda", "division_por_cero"),
    ({"resultado_operativo": -1_000, "depreciaciones_amortizaciones": 0}, "deuda_neta_ebitda", "ebitda_negativo"),
    ({"resultado_operativo": -1_000, "depreciaciones_amortizaciones": 1_000}, "deuda_neta_ebitda", "division_por_cero"),
    ({"caja_equivalentes": -5}, "cobertura_deuda_cp", "caja_equivalentes_negativa"),
    ({"deuda_financiera_no_corriente": -5}, "fcf_deuda", "deuda_financiera_no_corriente_negativa"),
])
def test_ratios_16_sin_dato_o_sin_sentido_quedan_null_con_motivo(cambios, ratio, motivo):
    datos = {k: v for k, v in {**COMPLETO, **cambios}.items() if v is not None}
    f = rat.ratios_de_periodo(periodo(**datos))
    assert f["ratios"][ratio] is None and f["motivos"][ratio] == motivo


def test_ratios_16_no_mezclan_unidades_ni_cifras_sospechosas():
    p = periodo(**COMPLETO)
    p["cifras"]["caja_equivalentes"]["valor"] = 2_000
    assert rat.ratios_de_periodo(p, {"caja_equivalentes"})["motivos"]["deuda_neta_ebitda"] == "cifra_sospechosa:caja_equivalentes"
    a = {c: rat.contexto(p, c) for c in rat.ENTRADAS["cobertura_deuda_cp"]}
    a["caja_equivalentes"] = {**a["caja_equivalentes"], "escala": 1}
    assert rat.calcular_ratio("cobertura_deuda_cp", a)[1] == "unidades_distintas"
