"""Fase 11: cruce con renta fija (cobertura por emisor y ON). Sin modelo; indices sinteticos."""
import csv
import datetime as dt
import gzip
import json

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import avisos_pago as avp
from cnv import cobertura as cob
from cnv import financieros as fin
from cnv import salud
from cnv.sincronizar import cargar_universo

HOY = dt.date(2026, 10, 16)
A = {"empresa": "EMISORA A S.A.", "cuit": "30546689979"}
B = {"empresa": "EMISORA B S.A.", "cuit": "30500010912"}
C = {"empresa": "SOLO ACCIONES S.A.", "cuit": "30707830270"}
VACIA = {"empresa": "VACIA S.A.", "cuit": "30657863056"}
UNIVERSO = [B, A, C, VACIA]
ONS = [
    {"ticker": "AA1O", "emisor": "A", "descripcion": "A Clase 1", "vence": "2028-01-01", "cuit": A["cuit"]},
    {"ticker": "AA2O", "emisor": "A", "descripcion": "A Clase 2", "vence": "2030-01-01", "cuit": A["cuit"]},
    {"ticker": "BB1O", "emisor": "B", "descripcion": "B Clase 1", "vence": "2027-06-01", "cuit": B["cuit"]},
    {"ticker": "ZZ1O", "emisor": "Z", "descripcion": "Z Clase 1", "vence": "2029-01-01", "cuit": "30111111112"},   # emisor fuera del universo
    {"ticker": "XX1O", "emisor": "X", "descripcion": "X Clase 1", "vence": None, "cuit": ""},                       # sin CUIT resuelto
]


def doc(i, t, f, ok=True, **kw):
    return {"id": str(i), "t": t, "f": f, "texto_ok": ok, **kw}


def pago(doc_, presentacion, fecha_pago, ticker=None, clase="Clase 1"):
    p = {"doc": doc_, "fecha_presentacion": presentacion, "fecha_pago": {"valor": fecha_pago}, "tipo_pago": {"valor": "interes"}, "clase": {"valor": clase}}
    if ticker:
        p["ticker"] = {"valor": ticker}
    return p


@pytest.fixture
def base(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    # A: todo cubierto
    alm.guardar_indice_empresa(a, A["cuit"], [doc(1, "estados_contables", "2026-08-10"), doc(2, "estados_contables", "2026-05-12"), doc(3, "prospectos", "2026-03-01"),
                                              doc(4, "avisos_pago", "2026-10-01"), doc(5, "avisos_pago", "2026-07-01"), doc(6, "hechos", "2026-10-10"), doc(7, "otros", "2026-10-11", ok=False)])
    fin.guardar_financieros(a, A["cuit"], {"periodos": [{"doc": "1", "periodo": "2026-06-30", "tipo": "semestral", "balance": "consolidado", "fecha_presentacion": "2026-08-10", "cifras": {"ventas": {}, "activo_total": {}}},
                                                       {"doc": "2", "periodo": "2026-03-31", "tipo": "trimestral", "balance": "consolidado", "fecha_presentacion": "2026-05-12", "cifras": {"ventas": {}}}], "procesados": {}})
    avp.guardar_avisos(a, A["cuit"], {"pagos": [pago("4", "2026-10-01", "2026-11-15", ticker="AA1O"), pago("5", "2026-07-01", "2026-08-15", ticker="AA1O"), pago("5", "2026-07-01", "2026-09-01", ticker="AA2O", clase="Clase 2")], "procesados": {}})
    # B: ON con muchos huecos
    alm.guardar_indice_empresa(a, B["cuit"], [doc(10, "estados_contables", "2025-01-10"), doc(11, "avisos_pago", "2026-04-01"), doc(12, "hechos", "2026-03-01", ok=False, intentos=3, error="sin adjunto"),
                                              doc(13, "actas", "2026-03-02", ok=False, intentos=0)])
    fin.guardar_financieros(a, B["cuit"], {"periodos": [{"doc": "10", "periodo": "2024-12-31", "tipo": "anual", "balance": "individual", "fecha_presentacion": "2025-01-10", "cifras": {"ventas": {}}}], "procesados": {}})
    # C: sin ONs; estados contables sin cifras
    alm.guardar_indice_empresa(a, C["cuit"], [doc(20, "estados_contables", "2026-09-01")])
    return a


def por_cuit(inf):
    return {e["cuit"]: e for e in inf["emisores"]}


def test_emisor_completo(base):
    inf = cob.calcular(base, UNIVERSO, ONS, HOY)
    e = por_cuit(inf)[A["cuit"]]
    assert [(o["ticker"], o["vence"], o["ultimo_pago"]) for o in e["ons"]] == [("AA1O", "2028-01-01", "2026-11-15"), ("AA2O", "2030-01-01", "2026-09-01")]
    assert e["documentos"]["estados_contables"] == {"total": 2, "con_texto": 2, "ultimo": "2026-08-10"}
    assert e["documentos"]["otros"] == {"total": 1, "con_texto": 0, "ultimo": "2026-10-11"}
    assert e["ultimo_estado_contable"] == {"doc": "1", "periodo": "2026-06-30", "tipo": "semestral", "balance": "consolidado", "fecha_presentacion": "2026-08-10", "cifras": 2}
    assert e["ultimo_aviso_pago"] == {"doc": "4", "fecha_presentacion": "2026-10-01", "fecha_pago": "2026-11-15", "tipo_pago": "interes", "clase": "Clase 1", "ticker": "AA1O", "estructurado": True}
    assert e["huecos"] == []


def test_emisor_con_huecos(base):
    e = por_cuit(cob.calcular(base, UNIVERSO, ONS, HOY))[B["cuit"]]
    assert e["huecos"] == ["estado_contable_desactualizado", "sin_prospectos", "avisos_de_pago_sin_estructurar", "documentos_fallidos", "documentos_sin_bajar", "sin_actividad_reciente"]
    assert e["ultimo_aviso_pago"] == {"doc": "11", "fecha_presentacion": "2026-04-01", "fecha_pago": None, "tipo_pago": None, "clase": None, "ticker": None, "estructurado": False}
    assert e["ons"] == [{"ticker": "BB1O", "vence": "2027-06-01", "ultimo_pago": None}]


def test_empresa_sin_ons_no_pide_prospectos_ni_avisos(base):
    e = por_cuit(cob.calcular(base, UNIVERSO, ONS, HOY))[C["cuit"]]
    assert e["ons"] == [] and e["huecos"] == ["estados_contables_sin_cifras"] and e["ultimo_estado_contable"] is None and e["ultimo_aviso_pago"] is None


def test_empresa_sin_ningun_documento(base):
    e = por_cuit(cob.calcular(base, UNIVERSO, ONS, HOY))[VACIA["cuit"]]
    assert e["huecos"] == ["sin_documentos", "sin_estados_contables"] and e["documentos"] == {}


def test_ons_que_no_se_pueden_vincular(base):
    inf = cob.calcular(base, UNIVERSO, ONS, HOY)
    assert inf["ons_sin_emisor"] == [{"ticker": "ZZ1O", "emisor": "Z", "cuit": "30111111112", "motivo": "emisor_fuera_del_universo"},
                                     {"ticker": "XX1O", "emisor": "X", "motivo": "sin_cuit_en_ons_csv"}]


def test_resumen(base):
    r = cob.calcular(base, UNIVERSO, ONS, HOY)["resumen"]
    assert r["empresas"] == 4 and r["emisores_con_ons"] == 2 and r["ons"] == 5 and r["ons_vinculadas"] == 3 and r["ons_sin_emisor"] == 2
    assert r["con_estado_contable"] == 2 and r["con_aviso_de_pago"] == 2 and r["emisores_con_ons_y_estado_contable"] == 2 and r["emisores_con_ons_y_aviso_de_pago"] == 2
    assert r["huecos"]["sin_estados_contables"] == 1 and r["huecos"]["documentos_fallidos"] == 1 and "sin_prospectos" in r["huecos"]


def test_umbral_de_estado_contable_viejo(base):
    def hueco(hoy):
        return "estado_contable_desactualizado" in por_cuit(cob.calcular(base, UNIVERSO, ONS, hoy))[A["cuit"]]["huecos"]
    cierre = dt.date(2026, 6, 30)
    assert not hueco(cierre + dt.timedelta(days=cob.DIAS_EC_VIEJO)) and hueco(cierre + dt.timedelta(days=cob.DIAS_EC_VIEJO + 1))


def test_emisores_ordenados_por_nombre_y_definiciones_de_huecos(base):
    inf = cob.calcular(base, UNIVERSO, ONS, HOY)
    assert [e["empresa"] for e in inf["emisores"]] == sorted(e["empresa"] for e in UNIVERSO)
    assert {h for e in inf["emisores"] for h in e["huecos"]} <= set(inf["huecos_definicion"])


def test_escribe_el_archivo_una_sola_vez(base):
    escrituras = []
    original = alm.AlmacenLocal.guardar
    alm.AlmacenLocal.guardar = lambda self, n, d: (escrituras.append(n), original(self, n, d))[1]
    try:
        cob.construir(base, UNIVERSO, ONS, HOY)
        cob.construir(base, UNIVERSO, ONS, HOY)
    finally:
        alm.AlmacenLocal.guardar = original
    assert escrituras == ["cnv/indice/cobertura.json.gz"]
    salida = json.loads(gzip.decompress(base.leer("cnv/indice/cobertura.json.gz")))
    assert salida["version"] == 1 and salida["al"] == "2026-10-16" and len(salida["emisores"]) == 4


def test_base_vacia(tmp_path):
    inf = cob.calcular(alm.AlmacenLocal(str(tmp_path)), [A], [], HOY)
    assert inf["resumen"]["emisores_con_ons"] == 0 and inf["emisores"][0]["huecos"] == ["sin_documentos", "sin_estados_contables"]
    assert cob.calcular(alm.AlmacenLocal(str(tmp_path)), [], ONS, HOY)["resumen"]["ons_sin_emisor"] == 5


def test_leer_ons_acepta_guiones_y_salta_filas_sin_ticker(tmp_path):
    ruta = tmp_path / "ons.csv"
    ruta.write_text("ticker,emisor,descripcion,vence,cuit,fuente_cuit\nAA1O,A,desc,2028-01-01,30-54668997-9,x\n,,,,,\nBB1O,B,desc,,,\n", encoding="utf-8")
    assert cob.leer_ons(str(ruta)) == [{"ticker": "AA1O", "emisor": "A", "descripcion": "desc", "vence": "2028-01-01", "cuit": "30546689979"},
                                       {"ticker": "BB1O", "emisor": "B", "descripcion": "desc", "vence": None, "cuit": ""}]


# --- los datos reales del repo ----------------------------------------------------------------------------

def test_data_ons_csv_es_consistente_con_el_universo():
    ons = cob.leer_ons()
    universo = {e["cuit"] for e in cargar_universo()}
    assert len(ons) >= 70 and len({o["ticker"] for o in ons}) == len(ons)
    assert all(o["cuit"] and salud.cuit_valido(o["cuit"]) for o in ons)
    assert [o["ticker"] for o in ons if o["cuit"] not in universo] == []      # si una ON queda fuera, hay que agregar al emisor al universo
    assert all(o["vence"] is None or dt.date.fromisoformat(o["vence"]) for o in ons)
    with open("data/ons.csv", encoding="utf-8", newline="") as f:
        assert next(csv.reader(f)) == ["ticker", "emisor", "descripcion", "vence", "cuit", "fuente_cuit"]


def test_cli_cobertura(base, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DATOS_LOCAL", str(base.raiz))
    monkeypatch.setattr(cli, "cargar_universo", lambda: UNIVERSO)
    ruta = tmp_path / "ons.csv"
    ruta.write_text("ticker,emisor,descripcion,vence,cuit\nAA1O,A,d,2028-01-01," + A["cuit"] + "\n", encoding="utf-8")
    assert cli.main(["cobertura", "--ons", str(ruta), "--hoy", "2026-10-16"]) == 0
    assert '"ons_vinculadas": 1' in capsys.readouterr().out and base.existe("cnv/indice/cobertura.json.gz")
