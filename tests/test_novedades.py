"""Fase 12: novedades (documentos nuevos entre corridas y lo presentado en los ultimos 7 dias)."""
import datetime as dt
import json

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import avisos_pago as avp
from cnv import novedades as nov

HOY = dt.date(2026, 10, 16)
A = {"empresa": "EMISORA A S.A.", "cuit": "30546689979"}
B = {"empresa": "EMISORA B S.A.", "cuit": "30500010912"}


def d(i, t, f, **kw):
    return {"id": str(i), "t": t, "f": f, "texto_ok": True, "u": f"https://x/{i}", **kw}


def pago(doc, pres, fecha_pago, ticker="AA1O", monto=1_000_000.0):
    return {"doc": doc, "fecha_presentacion": pres, "fecha_pago": {"valor": fecha_pago}, "tipo_pago": {"valor": "interes"}, "clase": {"valor": "Clase 1"},
            "ticker": {"valor": ticker}, "monto_total": {"valor": monto}, "moneda": {"valor": "pesos"}}


@pytest.fixture
def a(tmp_path):
    return alm.AlmacenLocal(str(tmp_path))


def estado_inicial(a):
    alm.guardar_indice_empresa(a, A["cuit"], [
        d(1, "hechos", "2026-10-15", d="Resultados del trimestre", r="Resumen 1", k="resultados"),
        d(2, "hechos", "2026-10-09", d="Dividendos", k="dividendos"),                   # justo en el limite de 7 dias: entra
        d(3, "hechos", "2026-10-08", d="Viejo", k="otro"),                              # 8 dias: no entra
        d(4, "hechos", "2026-10-14", d="Sin clasificar todavia"),
        d(5, "hechos", "2026-10-13", d="Categoria rara", k="inventada"),
        d(6, "avisos_pago", "2026-10-12", d="Pago de intereses"),
        d(7, "avisos_pago", "2026-10-02", d="Pago viejo"),
        d(8, "estados_contables", "2026-10-14", d="no se vigila"),
        d(9, "hechos", "2026-10-14", ok=False) | {"texto_ok": False, "d": "Sin texto"},
        d(10, "hechos", "2026-10-20", d="fecha futura: no entra"),
        d(11, "hechos", "", d="sin fecha"),
    ])
    avp.guardar_avisos(a, A["cuit"], {"pagos": [pago("6", "2026-10-12", "2026-11-15"), pago("6", "2026-10-12", "2026-11-15", ticker="AA2O"), pago("7", "2026-10-02", "2026-10-20")], "procesados": {}})
    alm.guardar_indice_empresa(a, B["cuit"], [d(20, "hechos", "2026-10-16", d="Hoy", k="calificacion_riesgo"), d(21, "avisos_pago", "2026-10-15", d="Pago sin estructurar")])


def leer(a):
    return json.loads(a.leer("cnv/indice/novedades.json"))


def test_primera_corrida_siembra_y_no_marca_nada_como_nuevo(a):
    estado_inicial(a)
    r = nov.construir(a, [A, B], HOY)
    assert r["nuevos_en_esta_corrida"] == 0
    vistos = json.loads(a.leer("cnv/indice/vistos.json"))
    assert vistos["version"] == 1 and vistos["cuits"][A["cuit"]]["1"] == "2026-10-15" and "8" not in vistos["cuits"][A["cuit"]]   # solo hechos y avisos, con la fecha de presentacion


def test_ventana_de_siete_dias_y_categorias(a):
    estado_inicial(a)
    nov.construir(a, [A, B], HOY)
    n = leer(a)
    assert (n["al"], n["desde"], n["dias"]) == ("2026-10-16", "2026-10-09", 7)
    assert list(n["hechos_por_categoria"]) == ["calificacion_riesgo", "dividendos", "resultados", "sin_clasificar"]
    assert [h["doc"] for h in n["hechos_por_categoria"]["sin_clasificar"]] == ["9", "4", "5"]      # mas nuevos primero; categoria invalida -> sin_clasificar
    docs = {h["doc"] for v in n["hechos_por_categoria"].values() for h in v}
    assert docs == {"1", "2", "4", "5", "9", "20"}                         # 3 (8 dias), 10 (futuro) y 11 (sin fecha) afuera; 8 no es hecho
    h1 = n["hechos_por_categoria"]["resultados"][0]
    assert h1 == {"cuit": A["cuit"], "empresa": A["empresa"], "doc": "1", "fecha": "2026-10-15", "titulo": "Resultados del trimestre", "resumen": "Resumen 1", "url": "https://x/1",
                  "categoria": "resultados", "detectado": "2026-10-15", "texto_ok": True}
    sin_texto = next(h for h in n["hechos_por_categoria"]["sin_clasificar"] if h["doc"] == "9")
    assert sin_texto["texto_ok"] is False and sin_texto["titulo"] == "Sin texto"       # un hecho sin texto igual es una novedad (titulo de la CNV)
    assert n["resumen"]["hechos"] == 6 and n["resumen"]["hechos_por_categoria"] == {"calificacion_riesgo": 1, "dividendos": 1, "resultados": 1, "sin_clasificar": 3}


def test_avisos_de_pago_estructurados_y_sin_estructurar(a):
    estado_inicial(a)
    nov.construir(a, [A, B], HOY)
    av = leer(a)["avisos_pago"]
    assert [(x["doc"], x["estructurado"], x["ticker"]) for x in av] == [("21", False, None), ("6", True, "AA1O"), ("6", True, "AA2O")]
    estructurados = [x for x in av if x["estructurado"]]
    assert {x["ticker"] for x in estructurados} == {"AA1O", "AA2O"} and all(x["doc"] == "6" for x in estructurados)
    assert estructurados[0]["fecha_pago"] == "2026-11-15" and estructurados[0]["monto_total"] == 1_000_000.0 and estructurados[0]["moneda"] == "pesos"
    sin = next(x for x in av if x["doc"] == "21")
    assert sin["estructurado"] is False and sin["fecha_pago"] is None and sin["empresa"] == B["empresa"]
    assert all(x["doc"] != "7" for x in av)                                # presentado hace 14 dias
    assert leer(a)["resumen"]["avisos_pago"] == 2 and leer(a)["resumen"]["pagos_estructurados"] == 2


def test_segunda_corrida_detecta_solo_lo_nuevo(a):
    estado_inicial(a)
    nov.construir(a, [A, B], HOY)
    idx = alm.leer_indice_empresa(a, A["cuit"]) + [d(30, "hechos", "2026-10-14", d="Nuevo hecho", k="emision_on"), d(31, "avisos_pago", "2026-10-15"), d(32, "estados_contables", "2026-10-16")]
    alm.guardar_indice_empresa(a, A["cuit"], idx)
    r = nov.construir(a, [A, B], dt.date(2026, 10, 17))
    assert r["nuevos_en_esta_corrida"] == 2                                  # el hecho 30 y el aviso 31; el estado contable no se vigila
    n = leer(a)
    nuevo = n["hechos_por_categoria"]["emision_on"][0]
    assert nuevo["doc"] == "30" and nuevo["detectado"] == "2026-10-17" and nuevo["fecha"] == "2026-10-14"        # presentado el 14, visto el 17
    viejo = n["hechos_por_categoria"]["resultados"][0]
    assert viejo["detectado"] == "2026-10-15"                                # lo sembrado conserva su fecha
    # una tercera corrida el mismo dia no vuelve a contar lo ya visto
    assert nov.construir(a, [A, B], dt.date(2026, 10, 17))["nuevos_en_esta_corrida"] == 0
    assert leer(a)["hechos_por_categoria"]["emision_on"][0]["detectado"] == "2026-10-17"


def test_carga_historica_no_inunda_la_lista(a):
    estado_inicial(a)
    nov.construir(a, [A, B], HOY)
    viejos = [d(100 + i, "hechos", "2024-01-05", d="historico", k="otro") for i in range(50)]
    alm.guardar_indice_empresa(a, A["cuit"], alm.leer_indice_empresa(a, A["cuit"]) + viejos)
    r = nov.construir(a, [A, B], dt.date(2026, 10, 17))
    assert r["nuevos_en_esta_corrida"] == 50 and "otro" not in leer(a)["hechos_por_categoria"]      # detectados hoy, pero presentados hace dos anios: no son novedad


def test_idempotente_no_reescribe_si_nada_cambio(a):
    estado_inicial(a)
    nov.construir(a, [A, B], HOY)
    antes = (a.leer("cnv/indice/novedades.json"), a.leer("cnv/indice/vistos.json"))
    escrituras = []
    original = alm.AlmacenLocal.guardar
    alm.AlmacenLocal.guardar = lambda self, n, x: (escrituras.append(n), original(self, n, x))[1]
    try:
        nov.construir(a, [A, B], HOY)
    finally:
        alm.AlmacenLocal.guardar = original
    assert escrituras == [] and (a.leer("cnv/indice/novedades.json"), a.leer("cnv/indice/vistos.json")) == antes


def test_ventana_configurable(a):
    estado_inicial(a)
    r = nov.construir(a, [A, B], HOY, dias=1)                                          # desde el 15: el hecho 1 (A) y el 20 (B)
    assert r["hechos"] == 2 and leer(a)["desde"] == "2026-10-15" and leer(a)["dias"] == 1


def test_vistos_ilegible_se_vuelve_a_sembrar_sin_romper(a):
    estado_inicial(a)
    a.guardar("cnv/indice/vistos.json", b"{ esto no es json")
    assert nov.construir(a, [A, B], HOY)["nuevos_en_esta_corrida"] == 0
    a.guardar("cnv/indice/vistos.json", json.dumps({"version": 99, "cuits": {}}).encode())
    assert nov.leer_vistos(a) is None and nov.construir(a, [A, B], HOY)["nuevos_en_esta_corrida"] == 0
    assert json.loads(a.leer("cnv/indice/vistos.json"))["version"] == 1


def test_base_vacia(a):
    r = nov.construir(a, [A], HOY)
    assert r["hechos"] == 0 and r["avisos_pago"] == 0 and leer(a)["hechos_por_categoria"] == {} and leer(a)["avisos_pago"] == []


def test_categorias_del_informe_son_las_de_clasificacion_mas_sin_clasificar():
    from cnv.clasificacion import CATEGORIAS

    assert nov.SIN_CLASIFICAR not in CATEGORIAS and "otro" in CATEGORIAS


def test_cli_novedades(a, monkeypatch, capsys):
    estado_inicial(a)
    monkeypatch.setenv("DATOS_LOCAL", str(a.raiz))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [A, B])
    assert cli.main(["novedades", "--hoy", "2026-10-16", "--dias", "3"]) == 0
    assert '"nuevos_en_esta_corrida": 0' in capsys.readouterr().out and leer(a)["dias"] == 3
