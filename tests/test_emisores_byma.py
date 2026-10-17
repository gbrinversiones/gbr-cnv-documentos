"""Fase 17: emisores de ONs de BYMA fuera del universo. Sin red: el autocompletado y la pagina de la CNV son dobles."""
import csv
import json

import pytest

from cnv import __main__ as cli
from cnv import emisores_byma as eby

FIXTURE = "tests/fixtures/ons_byma_muestra.json"
CUIT_YPF, CUIT_A = "30546689979", "30500010912"
CUIT_ARBOL, CUIT_PUENTES, CUIT_EDESA1, CUIT_EDESA2, CUIT_VM = "30707830278", "30657863056", "30525698412", "30696170580", "30663461113"
UNIVERSO = [{"empresa": "YPF", "cuit": CUIT_YPF, "origen": "BYMA"}, {"empresa": "EMISORA A S.A.", "cuit": CUIT_A, "origen": "BYMA"}]
ONS = [{"ticker": "AA1O", "emisor": "EMISORA A S.A.", "descripcion": "", "vence": "2028-01-01", "cuit": CUIT_A, "fuente_cuit": "universo"}]

AUTOCOMPLETADO = {   # termino -> resultados del autocompletado
    "arbol nuevo": [{"cuit": CUIT_ARBOL, "descripcion": "ARBOL NUEVO S.A.C.I.F.I.A."}, {"cuit": "30111111118", "descripcion": "ARBOL NUEVO HOLDING S.A."}],
    "puentes hermanos": [{"cuit": CUIT_PUENTES, "descripcion": "PUENTES HERMANOS S.A. (PUENTES)"}],
    "edesa": [{"cuit": CUIT_EDESA1, "descripcion": "EDESA S.A."}, {"cuit": CUIT_EDESA2, "descripcion": "EDESA S.A."}],
    "vaca muerta oil sur": [{"cuit": CUIT_VM, "descripcion": "VACA MUERTA OIL SUR S.A."}],
    "c&c medicals": [{"cuit": "30222222229", "descripcion": "C&C MEDICAL SERVICES S.A."}],
}
PAGINAS = {CUIT_ARBOL: "ÁRBOL NUEVO S.A.C.I.F.I.A.", CUIT_PUENTES: "PUENTES HERMANOS S.A.", CUIT_EDESA1: "EDESA S.A.", CUIT_VM: "OTRA RAZON DISTINTA S.A."}


def buscar(termino, s=None):
    k = eby.uni.normalizar(termino)
    if k == "boom":
        raise RuntimeError("cnv caida")
    for t, r in AUTOCOMPLETADO.items():
        if eby.uni.normalizar(t) == k or k.startswith(t):
            return r
    return []


def consultar(cuit, s=None, dormir=None):
    if cuit in PAGINAS:
        return {"estado": "ok", "razon_social": PAGINAS[cuit], "fragmento": "", "error": None}
    return {"estado": "sin_marcador", "razon_social": None, "fragmento": "", "error": None}


def plan(byma=None, universo=UNIVERSO, ons=ONS, **kw):
    return eby.planificar(eby.leer_byma(FIXTURE) if byma is None else byma, universo, ons, buscar=buscar, consultar=consultar, dormir=lambda _: None, pausa=0, **kw)


def test_leer_byma_solo_ticker_emisor_y_vencimiento_sin_repetidos():
    filas = eby.leer_byma(FIXTURE)
    assert [f["ticker"] for f in filas] == ["YM41O", "YM37O", "AA1O", "ZN1O", "ZN2O", "PH1D", "ED1O", "VM1O", "CC1O", "RR1O"]
    assert set(filas[0]) == {"ticker", "emisor", "vence"} and filas[4]["vence"] is None


@pytest.mark.parametrize("a,b,esperado", [
    ("YPF S.A.", "YPF", True), ("TELECOM ARGENTINA S.A.", "TELECOM ARGENTINA S. A.", True), ("MIRGOR S.A.C.I.F.I.A.", "MIRGOR S.A.", True),
    ("PUENTES HNOS S.A.", "PUENTES HERMANOS S.A.", True), ("EMPRESA DISTRIBUIDORA NORTE S.A. (EDENOR)", "EDENOR", False), ("EDENOR", "EMPRESA DISTRIBUIDORA NORTE S.A. (EDENOR)", True),
    ("EDESA S.A.", "EDESA HOLDING S.A.", False), ("", "X", False), ("CRESUD S.A.C.I.F.A.", "CRESUD SOCIEDAD ANONIMA", True),
])
def test_misma_razon_es_igualdad_exacta_de_la_clave_normalizada(a, b, esperado):
    assert eby.misma_razon(a, b) is esperado


def test_emisor_ya_en_el_universo_solo_suma_los_tickers_que_faltan_en_ons_csv():
    p = plan()
    ypf = [o for o in p["ons_nuevas"] if o["emisor"] == "YPF S.A."]
    assert [(o["ticker"], o["cuit"], o["fuente_cuit"], o["vence"]) for o in ypf] == [("YM37O", CUIT_YPF, "universo_exacto_byma", "2037-03-01"), ("YM41O", CUIT_YPF, "universo_exacto_byma", "2041-02-01")]
    assert not any(o["ticker"] == "AA1O" for o in p["ons_nuevas"])               # ya estaba en ons.csv
    assert CUIT_YPF not in {u["cuit"] for u in p["universo_nuevo"]}


def test_emisor_nuevo_con_coincidencia_exacta_y_pagina_que_confirma():
    p = plan()
    assert {(u["empresa"], u["cuit"], u["origen"]) for u in p["universo_nuevo"]} == {("ÁRBOL NUEVO S.A.C.I.F.I.A.", CUIT_ARBOL, "BYMA-ON"), ("PUENTES HERMANOS S.A.", CUIT_PUENTES, "BYMA-ON")}   # la razon que figura en la CNV
    arbol = [o for o in p["ons_nuevas"] if o["cuit"] == CUIT_ARBOL]
    assert [(o["ticker"], o["vence"], o["fuente_cuit"]) for o in arbol] == [("ZN1O", "2030-05-05", "cnv_autocompletado_exacto"), ("ZN2O", "", "cnv_autocompletado_exacto")]


@pytest.mark.parametrize("emisor,motivo", [
    ("EDESA S.A.", "varias_coincidencias_exactas"),                      # dos CUIT con la misma razon: ambiguo
    ("VACA MUERTA OIL SUR S.A.", "pagina_cnv_no_confirma"),              # autocompletado exacto pero la pagina dice otra cosa
    ("C&C MEDICALS S.A.", "sin_coincidencia_exacta_en_autocompletado"),  # solo parecidos
    ("RED CAIDA S.A.", "sin_coincidencia_exacta_en_autocompletado"),     # sin candidatos
])
def test_lo_dudoso_va_al_informe_y_nunca_al_csv(emisor, motivo):
    p = plan()
    e = next(x for x in p["por_revisar"] if x["emisor"] == emisor)
    assert e["motivo"] == motivo and e["tickers"]
    assert emisor not in {o["emisor"] for o in p["ons_nuevas"]} and emisor.split()[0] not in " ".join(u["empresa"] for u in p["universo_nuevo"])


def test_candidatos_del_informe_traen_cuit_descripcion_y_si_son_exactos():
    e = next(x for x in plan()["por_revisar"] if x["emisor"] == "C&C MEDICALS S.A.")
    assert [(c["cuit"], c["descripcion"], c["exacto"]) for c in e["candidatos"]] == [("30222222229", "C&C MEDICAL SERVICES S.A.", False)]
    assert next(x for x in plan()["por_revisar"] if x["emisor"] == "VACA MUERTA OIL SUR S.A.")["pagina_cnv"] == "OTRA RAZON DISTINTA S.A."


def test_cuit_con_digito_verificador_invalido_no_se_acepta():
    byma = [{"ticker": "ZZ1O", "emisor": "ARBOL NUEVO S.A.", "vence": None}]
    cuit_malo = "30707830270"
    original = dict(AUTOCOMPLETADO)
    AUTOCOMPLETADO["arbol nuevo"] = [{"cuit": cuit_malo, "descripcion": "ARBOL NUEVO S.A."}]
    try:
        p = plan(byma=byma)
    finally:
        AUTOCOMPLETADO.clear()
        AUTOCOMPLETADO.update(original)
    assert p["por_revisar"][0]["motivo"] == "cuit_invalido" and p["universo_nuevo"] == []


def test_la_cnv_caida_no_confirma_nada():
    byma = [{"ticker": "ZZ1O", "emisor": "BOOM", "vence": None}]
    p = plan(byma=byma)
    assert p["por_revisar"][0]["motivo"] == "cnv_no_responde" and p["universo_nuevo"] == [] and p["ons_nuevas"] == []


def test_emisor_nuevo_cuyo_cuit_ya_esta_en_el_universo_con_otro_nombre_solo_suma_la_on():
    universo = [*UNIVERSO, {"empresa": "ARBOL NUEVO HOLDING VIEJO", "cuit": CUIT_ARBOL, "origen": "BYMA"}]
    p = plan(universo=universo)
    assert CUIT_ARBOL not in {u["cuit"] for u in p["universo_nuevo"]} and any(o["ticker"] == "ZN1O" and o["cuit"] == CUIT_ARBOL for o in p["ons_nuevas"])


def test_resumen_y_tope_de_tiempo():
    r = plan()["resumen"]
    assert r == {"emisores_en_byma": 8, "ya_en_universo": 2, "emisores_nuevos_confirmados": 2, "ons_nuevas": 5, "emisores_por_revisar": 4, "emisores_sin_tickers_nuevos": 0, "pendientes_por_tiempo": 0}
    vencido = plan(hasta_t=0.0001)
    assert vencido["resumen"]["pendientes_por_tiempo"] == 6 and vencido["universo_nuevo"] == [] and vencido["por_revisar"] == []


def test_informe_determinista_y_sin_fechas(tmp_path):
    p = plan()
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    eby.escribir_informe(p, "ons_byma.json", str(a))
    eby.escribir_informe(plan(), "ons_byma.json", str(b))
    assert a.read_bytes() == b.read_bytes()
    d = json.loads(a.read_text(encoding="utf-8"))
    assert d["version"] == 1 and len(d["emisores"]) == 4 and "generado" not in d and a.read_text(encoding="utf-8").endswith("\n")


def test_escribir_csv_agrega_al_final_sin_tocar_lo_existente(tmp_path):
    uni_csv, ons_csv = tmp_path / "universo.csv", tmp_path / "ons.csv"
    uni_csv.write_text("empresa,cuit,origen\nYPF,30546689979,BYMA", encoding="utf-8")          # sin salto de linea final
    ons_csv.write_text("ticker,emisor,descripcion,vence,cuit,fuente_cuit\nAA1O,EMISORA A S.A.,,2028-01-01,30500010912,universo\n", encoding="utf-8")
    p = plan()
    assert eby.escribir_csv(p, str(uni_csv), str(ons_csv)) == {"universo": 2, "ons": 5}
    filas = list(csv.DictReader(uni_csv.open(encoding="utf-8")))
    assert [f["empresa"] for f in filas][0] == "YPF" and filas[-1]["origen"] == "BYMA-ON" and len(filas) == 3
    ons = list(csv.DictReader(ons_csv.open(encoding="utf-8")))
    assert ons[0]["ticker"] == "AA1O" and len(ons) == 6 and {o["fuente_cuit"] for o in ons[1:]} == {"universo_exacto_byma", "cnv_autocompletado_exacto"}
    assert eby.escribir_csv({"universo_nuevo": [], "ons_nuevas": []}, str(uni_csv), str(ons_csv)) == {"universo": 0, "ons": 0}


def test_los_csv_resultantes_pasan_las_validaciones_del_repo():
    from cnv.salud import cuit_valido
    p = plan()
    assert all(cuit_valido(u["cuit"]) for u in p["universo_nuevo"]) and all(cuit_valido(o["cuit"]) for o in p["ons_nuevas"])


def test_comando_informa_y_solo_escribe_con_escribir(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "universo.csv").write_text("empresa,cuit,origen\nYPF,30546689979,BYMA\n", encoding="utf-8")
    (tmp_path / "data" / "ons.csv").write_text("ticker,emisor,descripcion,vence,cuit,fuente_cuit\n", encoding="utf-8")
    import shutil
    shutil.copy(__import__("os").path.join(__import__("os").path.dirname(__file__), "fixtures", "ons_byma_muestra.json"), tmp_path / "ons_byma.json")
    monkeypatch.setattr(eby.uni, "buscar_autocompletado", buscar)
    monkeypatch.setattr(eby, "planificar", plan_real)
    assert cli.main(["universo", "--sugerir-desde-byma", "ons_byma.json", "--ons", "data/ons.csv"]) == 0
    assert "A revisar a mano" in capsys.readouterr().out
    assert (tmp_path / "informes" / "emisores_por_revisar.json").exists()
    assert (tmp_path / "data" / "ons.csv").read_text(encoding="utf-8").count("\n") == 1          # sin --escribir no cambia nada
    assert cli.main(["universo", "--sugerir-desde-byma", "ons_byma.json", "--ons", "data/ons.csv", "--escribir"]) == 0
    assert (tmp_path / "data" / "ons.csv").read_text(encoding="utf-8").count("\n") > 1
    assert cli.main(["universo"]) == 2


_planificar_real = eby.planificar


def plan_real(byma, universo, ons, **k):
    return _planificar_real(byma, universo, ons, buscar=buscar, consultar=consultar, dormir=lambda _: None, pausa=0, hasta_t=k.get("hasta_t"))
