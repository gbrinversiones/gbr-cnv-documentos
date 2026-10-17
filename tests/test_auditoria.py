"""Fase 14: auditoria contable de las cifras y su efecto en los ratios (las cifras sospechosas quedan afuera)."""
import gzip
import json

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import auditoria as aud
from cnv import financieros as fin
from cnv import ratios as rat

from .test_ratios import COMPLETO, periodo

EMP = {"empresa": "EMPRESA EJEMPLO S.A.", "cuit": "30111111112"}
OTRA = {"empresa": "OTRA S.A.", "cuit": "30500010912"}


def reglas(hs):
    return sorted((h["regla"], tuple(h["campos"])) for h in hs)


# --- dentro de un periodo ---------------------------------------------------------------------

def test_un_balance_sano_no_tiene_hallazgos():
    assert aud.auditar_periodo(periodo(**COMPLETO)) == []


@pytest.mark.parametrize("pasivo,patrimonio,marca", [(21_000, 14_000, False), (21_300, 14_000, False), (21_350, 14_000, False), (21_351, 14_000, True), (10_000, 14_000, True), (21_000, 9_000, True)])
def test_balance_no_cuadra_con_tolerancia_del_1_por_ciento(pasivo, patrimonio, marca):
    p = periodo(activo_total=35_000, pasivo_total=pasivo, patrimonio_neto=patrimonio)
    hs = aud.auditar_periodo(p)
    assert bool(hs) is marca
    if marca:
        assert hs[0]["regla"] == "balance_no_cuadra" and hs[0]["campos"] == ["activo_total", "pasivo_total", "patrimonio_neto"] and hs[0]["severidad"] == "sospechosa" and "diferencia" in hs[0]["detalle"]


def test_balance_con_patrimonio_negativo_que_cuadra_es_valido():
    assert aud.auditar_periodo(periodo(activo_total=10_000, pasivo_total=12_000, patrimonio_neto=-2_000)) == []      # pasivo a descubierto: es raro pero real


def test_balance_incompleto_o_en_cero_no_se_audita():
    assert aud.auditar_periodo(periodo(activo_total=10_000, pasivo_total=5_000)) == []
    assert aud.auditar_periodo(periodo(activo_total=0, pasivo_total=5, patrimonio_neto=1)) == []


def test_resultado_mayor_que_ventas():
    hs = aud.auditar_periodo(periodo(ventas=1_000, resultado_neto=1_500, resultado_operativo=1_200))
    assert reglas(hs) == [("resultado_neto_mayor_que_ventas", ("resultado_neto", "ventas")), ("resultado_operativo_mayor_que_ventas", ("resultado_operativo", "ventas"))]
    assert aud.auditar_periodo(periodo(ventas=1_000, resultado_neto=1_000)) == []                  # igual no es mayor
    assert aud.auditar_periodo(periodo(ventas=0, resultado_neto=50)) == []                         # sin ventas no hay con que comparar
    assert aud.auditar_periodo(periodo(ventas=1_000, resultado_neto=-9_000)) == []                 # una perdida grande no es este error


@pytest.mark.parametrize("campo", ["ventas", "activo_total", "pasivo_total", "efectivo", "deuda_financiera", "activo_corriente", "pasivo_corriente", "depreciaciones_amortizaciones"])
def test_signos_raros(campo):
    hs = aud.auditar_periodo(periodo(**{campo: -5}))
    assert [(h["regla"], h["campos"]) for h in hs] == [("signo_raro", [campo])]


def test_los_campos_que_pueden_ser_negativos_no_se_marcan():
    assert aud.auditar_periodo(periodo(resultado_neto=-500, resultado_operativo=-300, patrimonio_neto=-100, intereses=-50)) == []


@pytest.mark.parametrize("parte,todo", [("activo_corriente", "activo_total"), ("pasivo_corriente", "pasivo_total"), ("efectivo", "activo_total"), ("deuda_financiera", "pasivo_total")])
def test_una_parte_no_puede_superar_al_todo(parte, todo):
    hs = aud.auditar_periodo(periodo(**{parte: 1_020, todo: 1_000}))
    assert reglas(hs) == [("parte_mayor_que_el_todo", tuple(sorted((parte, todo))))]
    assert aud.auditar_periodo(periodo(**{parte: 1_005, todo: 1_000})) == []                    # dentro del 1%: redondeos


# --- entre periodos del mismo emisor -------------------------------------------------------------------

def serie(*valores, campo="activo_total", escala=1000, moneda="pesos", balance="consolidado", inicio=2020):
    return [periodo(doc=str(10 + i), periodo_=f"{inicio + i}-12-31", escala=escala, moneda=moneda, balance=balance, **{campo: v}) for i, v in enumerate(valores)]


def test_serie_estable_no_tiene_hallazgos():
    assert aud.auditar_serie(serie(100, 120, 150, 400)) == []


def test_salto_de_mas_de_50_veces_entre_dos_periodos_marca_los_dos():
    hs = aud.auditar_serie(serie(100, 5_100))
    assert [(h["regla"], h["doc"]) for h in hs] == [("salto_entre_periodos", "10"), ("salto_entre_periodos", "11")]       # con dos puntos no se sabe cual esta mal
    assert aud.auditar_serie(serie(100, 5_000)) == []                                                                # 50 veces justo: no
    assert [h["regla"] for h in aud.auditar_serie(serie(5_100, 100))] == ["salto_entre_periodos"] * 2                # y hacia abajo tambien


def test_en_un_pico_solo_se_marca_el_periodo_atipico():
    hs = aud.auditar_serie(serie(100, 120, 150_000, 140, 160))
    assert {h["doc"] for h in hs} == {"12"} and all(h["campos"] == ["activo_total"] for h in hs)                   # los dos saltos (subida y bajada) senalan al mismo


def test_en_un_escalon_se_marca_el_que_esta_lejos_de_la_mediana():
    hs = aud.auditar_serie(serie(100, 110, 120, 130_000, 135_000, 140_000, 150_000))
    assert {h["doc"] for h in hs} == {"12"}          # el escalon aparece en el doc 12 (primero que cambia); los siguientes ya estan cerca de la mediana


def test_un_error_de_escala_se_reporta_como_unidad_incoherente():
    ps = serie(100_000, 110_000, 120_000)
    ps[2] = periodo(doc="12", periodo_="2022-12-31", escala=1_000_000, activo_total=120_000)                          # declaro millones en vez de miles: x1000
    hs = aud.auditar_serie(ps)
    assert [(h["regla"], h["doc"], h["severidad"]) for h in hs if h["severidad"] == "sospechosa"] == [("unidad_incoherente", "12", "sospechosa")]
    assert any(h["regla"] == "escala_cambia" and h["severidad"] == "info" for h in hs)


def test_cambio_de_escala_con_valores_absolutos_comparables_es_solo_informativo():
    ps = serie(100_000, 110_000)                                                                    # en miles: 100 millones
    ps.append(periodo(doc="12", periodo_="2022-12-31", escala=1_000_000, activo_total=120))          # en millones: 120 millones -> coherente
    hs = aud.auditar_serie(ps)
    assert [(h["regla"], h["severidad"], h["campos"]) for h in hs] == [("escala_cambia", "info", [])]


def test_cambio_de_moneda_no_se_compara_y_se_informa():
    ps = serie(100, 120) + [periodo(doc="12", periodo_="2022-12-31", moneda="dolares", escala=1000, activo_total=9_999_999)]
    hs = aud.auditar_serie(ps)
    assert [(h["regla"], h["severidad"]) for h in hs] == [("moneda_cambia", "info")]


def test_consolidado_e_individual_se_auditan_por_separado():
    ps = serie(100, 120, 140, balance="consolidado") + serie(5, 6, 7, balance="individual")
    assert aud.auditar_serie(ps) == []


def test_ceros_y_cifras_ausentes_no_dan_saltos():
    assert aud.auditar_serie(serie(0, 100, 120)) == [] and aud.auditar_serie(serie(100)) == []
    sin_escala = serie(100, 9_999_999)
    sin_escala[1]["escala"] = None
    assert [h for h in aud.auditar_serie(sin_escala) if h["severidad"] == "sospechosa"] == []     # sin escala declarada no se puede pasar a unidades absolutas (solo queda el aviso de que cambio)


# --- informe completo y archivo ---------------------------------------------------------------------

def guardar(a, periodos, cuit=EMP["cuit"]):
    fin.guardar_financieros(a, cuit, {"periodos": periodos, "procesados": {}})


@pytest.fixture
def base(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    malo = periodo(doc="11", periodo_="2025-06-30", tipo="semestral", activo_total=35_000, pasivo_total=10_000, patrimonio_neto=14_000, ventas=1_000, resultado_neto=2_000)
    guardar(a, [periodo(doc="10", **COMPLETO), malo])
    guardar(a, [periodo(doc="20", **COMPLETO)], cuit=OTRA["cuit"])
    return a


def test_informe_completo(base):
    inf = aud.auditar(base, [EMP, OTRA, {"empresa": "SIN DATOS", "cuit": "30999999999"}])
    assert inf["version"] == 1 and inf["resumen"]["periodos"] == 3
    assert inf["resumen"]["por_regla"] == {"balance_no_cuadra": 1, "resultado_neto_mayor_que_ventas": 1}
    assert inf["sospechosas"] == {EMP["cuit"]: {"11": ["activo_total", "pasivo_total", "patrimonio_neto", "resultado_neto", "ventas"]}}
    assert inf["resumen"]["periodos_con_cifras_sospechosas"] == 1 and inf["resumen"]["cifras_sospechosas"] == 5
    h = inf["hallazgos"][0]
    assert {"cuit", "empresa", "doc", "periodo", "tipo", "balance", "regla", "severidad", "campos", "detalle"} <= set(h) and h["cuit"] == EMP["cuit"] and h["doc"] == "11"


def test_los_hallazgos_informativos_no_marcan_cifras(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    guardar(a, serie(100, 110) + [periodo(doc="12", periodo_="2022-12-31", moneda="dolares", activo_total=1)])
    inf = aud.auditar(a, [EMP])
    assert inf["resumen"]["por_regla"] == {"moneda_cambia": 1} and inf["sospechosas"] == {}


def test_el_archivo_no_se_reescribe_si_nada_cambio_y_se_lee_de_vuelta(base):
    aud.construir(base, [EMP, OTRA])
    antes = base.leer("cnv/indice/auditoria_cifras.json.gz")
    escrituras = []
    original = alm.AlmacenLocal.guardar
    alm.AlmacenLocal.guardar = lambda self, n, d: (escrituras.append(n), original(self, n, d))[1]
    try:
        aud.construir(base, [EMP, OTRA])
    finally:
        alm.AlmacenLocal.guardar = original
    assert escrituras == [] and base.leer("cnv/indice/auditoria_cifras.json.gz") == antes
    assert aud.leer_sospechosas(base) == {EMP["cuit"]: {"11": {"activo_total", "pasivo_total", "patrimonio_neto", "resultado_neto", "ventas"}}}


def test_leer_sospechosas_tolera_archivo_ausente_roto_o_de_otra_version(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    assert aud.leer_sospechosas(a) == {}
    a.guardar("cnv/indice/auditoria_cifras.json.gz", b"no es gzip")
    assert aud.leer_sospechosas(a) == {}
    a.guardar("cnv/indice/auditoria_cifras.json.gz", alm.comprimir(json.dumps({"version": 99, "sospechosas": {"1": {"2": ["ventas"]}}}).encode()))
    assert aud.leer_sospechosas(a) == {}
    a.guardar("cnv/indice/auditoria_cifras.json.gz", alm.comprimir(b'{"version": 1}'))
    assert aud.leer_sospechosas(a) == {}


# --- efecto en los ratios -----------------------------------------------------------------------------------

def test_un_ratio_con_una_cifra_sospechosa_queda_null_con_motivo():
    f = rat.ratios_de_periodo(periodo(**COMPLETO), {"patrimonio_neto"})
    assert f["ratios"]["endeudamiento"] is None and f["motivos"]["endeudamiento"] == "cifra_sospechosa:patrimonio_neto"
    assert f["ratios"]["roe"] is None and f["motivos"]["roe"] == "cifra_sospechosa:patrimonio_neto"
    assert f["ratios"]["liquidez_corriente"] == 1.5 and f["ratios"]["margen_neto"] == 0.07 and f["ratios"]["cobertura_intereses"] == 2.0     # los que no la usan siguen
    assert f["sospechosas"] == ["patrimonio_neto"] and "patrimonio_neto" not in f["insumos"]


def test_sin_sospechosas_el_resultado_es_el_de_siempre():
    f = rat.ratios_de_periodo(periodo(**COMPLETO))
    assert "sospechosas" not in f and f["motivos"] == {} and f == rat.ratios_de_periodo(periodo(**COMPLETO), frozenset())


def test_varias_cifras_sospechosas_y_el_motivo_las_lista():
    f = rat.ratios_de_periodo(periodo(**COMPLETO), {"activo_corriente", "pasivo_corriente"})
    assert f["motivos"]["liquidez_corriente"] == "cifra_sospechosa:activo_corriente,pasivo_corriente" and f["sospechosas"] == ["activo_corriente", "pasivo_corriente"]


def test_cifra_sospechosa_que_ademas_falta_en_el_periodo_no_se_reporta_como_sospechosa():
    sin = {k: v for k, v in COMPLETO.items() if k != "ventas"}
    f = rat.ratios_de_periodo(periodo(**sin), {"ventas"})
    assert f["motivos"]["margen_neto"] == "cifra_sospechosa:ventas" and "ventas" not in f.get("sospechosas", [])


def test_ratios_lee_la_auditoria_del_almacen(base):
    aud.construir(base, [EMP, OTRA])
    stats = rat.construir(base, [EMP, OTRA])
    salida = json.loads(gzip.decompress(base.leer("cnv/indice/ratios.json.gz")))
    assert salida["auditoria_cifras"] is True
    fila = next(f for f in salida["emisores"][EMP["cuit"]]["periodos"] if f["doc"] == "11")
    assert fila["motivos"]["margen_neto"] == "cifra_sospechosa:resultado_neto,ventas" and fila["motivos"]["endeudamiento"].startswith("cifra_sospechosa")
    sano = next(f for f in salida["emisores"][OTRA["cuit"]]["periodos"])
    assert "sospechosas" not in sano and sano["ratios"]["margen_neto"] == 0.07
    assert stats["motivos"]["cifra_sospechosa"] >= 2


def test_ratios_sin_auditoria_previa_funciona_como_antes(base):
    rat.construir(base, [EMP, OTRA])
    salida = json.loads(gzip.decompress(base.leer("cnv/indice/ratios.json.gz")))
    assert salida["auditoria_cifras"] is False
    assert all("sospechosas" not in f for e in salida["emisores"].values() for f in e["periodos"])


def test_cli_auditar_cifras(base, monkeypatch, capsys):
    monkeypatch.setenv("DATOS_LOCAL", str(base.raiz))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP, OTRA])
    assert cli.main(["auditar-cifras"]) == 0
    assert '"hallazgos": 2' in capsys.readouterr().out and base.existe("cnv/indice/auditoria_cifras.json.gz")
