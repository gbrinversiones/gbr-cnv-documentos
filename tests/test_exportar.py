"""Fase 15: exportables CSV. Sin modelo; almacen local con datos sinteticos."""
import csv
import io
import json

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import cobertura as cob
from cnv import condiciones as cond
from cnv import exportar as exp
from cnv import financieros as fin
from cnv import avisos_pago as avp

A = {"empresa": "EMISORA A S.A.", "cuit": "30546689979"}
B = {"empresa": "EMISORA B S.A.", "cuit": "30500010912"}
UNIVERSO = [B, A]


def c(v, p=1):
    return {"valor": v, "pagina": p, "cita": "x"}


def periodo(doc, cierre, tipo="anual", **cifras):
    return {"doc": doc, "periodo": cierre, "tipo": tipo, "acumulado": True, "balance": "consolidado", "moneda": "pesos", "unidad": "miles de pesos", "escala": 1000,
            "fecha_presentacion": "2026-03-01", "periodo_verificado": True, "balance_coherente": True, "cifras": {k: c(v) for k, v in cifras.items()}}


def leer_csv(almacen, nombre):
    crudo = almacen.leer("cnv/export/" + nombre)
    assert crudo.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(crudo.decode("utf-8-sig"))))


@pytest.fixture
def base(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, A["cuit"], [
        {"id": "5", "t": "hechos", "f": "2026-10-01", "texto_ok": True, "d": "Emision de ON, con \"comillas\", y coma", "r": "Resumen, con coma", "k": "deuda", "u": "http://x/5"},
        {"id": "6", "t": "hechos", "f": "2026-10-05", "texto_ok": False, "d": "Otro hecho", "u": "http://x/6"},
        {"id": "7", "t": "actas", "f": "2026-10-05", "texto_ok": True, "d": "acta"}])
    fin.guardar_financieros(a, A["cuit"], {"periodos": [
        periodo("1", "2025-12-31", activo_corriente=200, pasivo_corriente=100, ventas=1000, resultado_neto=100, pasivo_total=300, patrimonio_neto=600),
        periodo("2", "2025-09-30", "trimestral", ventas=500)], "procesados": {}})
    a.guardar("cnv/indice/auditoria_cifras.json.gz", alm.comprimir(json.dumps({"version": 1, "sospechosas": {A["cuit"]: {"2": ["ventas"]}}}).encode()))
    avisos = [{"cuit": A["cuit"], "empresa": A["empresa"], "doc": "9", "fecha": "2026-10-02", "emisor": "A", "fuente": "pdf", "serie_cnv": None, "clase": "Clase 1", "ticker": "AA1O",
               "tipo_pago": "interes", "fecha_pago": "2026-11-15", "monto_total": 1234.5, "moneda": "pesos", "valor_residual": 100.0, "tasa": 7.5, "valor_residual_unidad": "%", "tasa_unidad": "%", "ambiguo": ["tasa", "monto_total"]}]
    a.guardar("cnv/indice/avisos_pago.json.gz", alm.comprimir(json.dumps(avisos).encode()))
    cond_fila = {"cuit": A["cuit"], "empresa": A["empresa"], "doc": "3", "n": 0, "tipo_documento": "suplemento", "fecha_presentacion": "2026-09-01", "url": "http://x/3", "clase": "XLIV",
                 "tasa_tipo": "fija", "tasa_valor": 7.55, "fecha_vencimiento": "2035-06-18", "monto_maximo": 500000000.0, "ticker": "AA1O", "tickers": ["AA1O"], "ambiguo": None}
    a.guardar("cnv/indice/condiciones.json.gz", alm.comprimir(json.dumps({"version": 1, "filas": [cond_fila], "ons": {}}).encode()))
    cob.construir(a, UNIVERSO, [{"ticker": "AA1O", "emisor": "A", "descripcion": "", "vence": "2035-06-18", "cuit": A["cuit"]}])
    return a


def test_todas_las_columnas_tienen_descripcion():
    for nombre, cols in exp.COLUMNAS.items():
        assert len(set(cols)) == len(cols)
        assert set(cols) == set(exp.DESCRIPCIONES[nombre]), nombre
    md = exp.diccionario().decode()
    for nombre, cols in exp.COLUMNAS.items():
        assert f"## {nombre}" in md and all(f"| `{x}` |" in md for x in cols)


def test_financieros_cifras_ratios_y_sospechosas(base):
    r = exp.construir(base, UNIVERSO)
    assert r["financieros.csv"] == 2
    filas = {f["doc"]: f for f in leer_csv(base, "financieros.csv")}
    anual = filas["1"]
    assert (anual["empresa"], anual["periodo"], anual["tipo"], anual["escala"], anual["activo_corriente"], anual["ventas"]) == (A["empresa"], "2025-12-31", "anual", "1000", "200", "1000")
    assert anual["liquidez_corriente"] == "2.0" and anual["margen_neto"] == "0.1" and anual["endeudamiento"] == "0.5"
    assert anual["acumulado"] == "true" and anual["efectivo"] == ""
    assert "deuda_ebitda:falta_cifra" in anual["ratios_sin_calcular"]
    trimestre = filas["2"]
    assert trimestre["ventas"] == "" and trimestre["cifras_sospechosas"] == "ventas"      # la cifra marcada por la auditoria no se exporta


def test_hechos_condiciones_avisos_cobertura(base):
    exp.construir(base, UNIVERSO)
    h = leer_csv(base, "hechos.csv")
    assert [x["doc"] for x in h] == ["6", "5"] and h[1]["titulo"] == 'Emision de ON, con "comillas", y coma' and h[1]["categoria"] == "deuda" and h[0]["categoria"] == "" and h[0]["texto_ok"] == "false"
    assert leer_csv(base, "condiciones.csv")[0]["tasa_valor"] == "7.55" and leer_csv(base, "condiciones.csv")[0]["ticker"] == "AA1O"
    av = leer_csv(base, "avisos_pago.csv")[0]
    assert av["monto_total"] == "1234.5" and av["ambiguo"] == "tasa;monto_total" and av["fecha_pago"] == "2026-11-15"
    cb = {x["cuit"]: x for x in leer_csv(base, "cobertura.csv")}
    assert cb[A["cuit"]]["ons"] == "AA1O" and cb[A["cuit"]]["cantidad_ons"] == "1" and cb[A["cuit"]]["documentos"] == "3" and cb[A["cuit"]]["ultimo_estado_contable_periodo"] == "2025-12-31"
    assert cb[B["cuit"]]["huecos"] == "sin_documentos;sin_estados_contables"


def test_idempotente_y_sin_reescribir(base):
    primero = exp.construir(base, UNIVERSO)
    assert primero["escritos"] == 6
    antes = {n: base.leer("cnv/export/" + n) for n in [*exp.COLUMNAS, "diccionario.md"]}
    assert exp.construir(base, UNIVERSO)["escritos"] == 0
    assert antes == {n: base.leer("cnv/export/" + n) for n in antes}


def test_sin_indices_solo_encabezado(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    exp.construir(a, UNIVERSO)
    assert a.leer("cnv/export/avisos_pago.csv").decode("utf-8-sig").strip() == ",".join(exp.COLUMNAS["avisos_pago.csv"])
    assert leer_csv(a, "financieros.csv") == []


def test_indice_ilegible_no_rompe(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    a.guardar("cnv/indice/cobertura.json.gz", b"no es gzip")
    assert exp.construir(a, UNIVERSO)["cobertura.csv"] == 0


def test_celdas():
    assert exp._celda(None) == "" and exp._celda(True) == "true" and exp._celda(["a", "b"]) == "a;b" and exp._celda(0) == 0
    assert exp.a_csv(["x", "y"], [{"x": 1, "z": 9}]) == b"\xef\xbb\xbfx,y\n1,\n"


def test_comando(base, monkeypatch, capsys):
    monkeypatch.setattr(cli, "almacen_desde_entorno", lambda: base, raising=False)
    monkeypatch.setattr(cli.alm, "almacen_desde_entorno", lambda: base)
    monkeypatch.setattr(cli, "cargar_universo", lambda: UNIVERSO)
    assert cli.main(["exportar"]) == 0
    assert "financieros.csv" in capsys.readouterr().out


def test_un_total_derivado_no_se_exporta_como_si_fuera_una_cifra_del_documento(base):
    fin.guardar_financieros(base, A["cuit"], {"periodos": [periodo("9", "2025-12-31", deuda_financiera_corriente=4000, deuda_financiera_no_corriente=8000, resultado_operativo=3000, depreciaciones_amortizaciones=1000)], "procesados": {}})
    exp.construir(base, UNIVERSO)
    f = {x["doc"]: x for x in leer_csv(base, "financieros.csv")}["9"]
    assert f["deuda_financiera"] == "" and f["deuda_financiera_corriente"] == "4000" and f["deuda_financiera_no_corriente"] == "8000"
    assert f["deuda_ebitda"] == "3.0"                  # el ratio si usa la suma, y lo dice en derivados
