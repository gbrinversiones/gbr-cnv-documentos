"""Fase 6: verificacion del universo y sugerencias. La red se reemplaza por dobles; el CSV nunca se modifica."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from cnv import __main__ as cli
from cnv import salud
from cnv import universo as uni

AQUI = os.path.dirname(__file__)
OK = open(os.path.join(AQUI, "fixtures", "empresa_cnv_ok.html"), encoding="utf-8").read()
SIN_RESULTADOS = open(os.path.join(AQUI, "fixtures", "empresa_cnv_sin_resultados.html"), encoding="utf-8").read()
PORTADA = open(os.path.join(AQUI, "fixtures", "portada_cnv_generica.html"), encoding="utf-8").read()


def cuit_con_digito(base10):
    resto = 11 - sum(int(d) * p for d, p in zip(base10, (5, 4, 3, 2, 7, 6, 5, 4, 3, 2))) % 11
    return base10 + str({11: 0, 10: 9}.get(resto, resto))


C1, C2, C3, C4 = (cuit_con_digito(b) for b in ("3052655116", "3050001091", "3070783027", "3065786305"))


class Sesion:
    """Doble de requests.Session: `paginas[cuit]` es HTML, una excepcion o un codigo HTTP; `auto[termino]` la respuesta del autocompletado."""

    def __init__(self, paginas=None, auto=None):
        self.paginas, self.auto, self.pedidos = paginas or {}, auto or {}, []

    def get(self, url, params=None, timeout=None):
        self.pedidos.append((url, params))
        if "Autocomplete" in url:
            x = self.auto.get(params["term"], [])
            if isinstance(x, Exception):
                raise x
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: x)
        cuit = url.split("/Empresa/")[1].split("?")[0]
        x = self.paginas[cuit]
        if isinstance(x, list):  # secuencia de respuestas por intento
            x = x.pop(0)
        if isinstance(x, Exception):
            raise x
        def falla():
            if isinstance(x, int):
                raise RuntimeError(f"http {x}")
        return SimpleNamespace(text=x if isinstance(x, str) else "", encoding=None, raise_for_status=falla)


def no_dormir(t):
    pass


# --- pagina de la CNV ---------------------------------------------------------

def test_razon_social_de_la_pagina():
    assert uni.razon_social_de_pagina(OK)[:2] == ("ok", "PAMPA ENERGIA S.A.")  # ignora el <script> con una razon falsa
    assert uni.razon_social_de_pagina(PORTADA) == ("sin_marcador", None, "")
    estado, razon, frag = uni.razon_social_de_pagina("<p>Resultados de búsqueda: algo raro</p>")
    assert (estado, razon) == ("sin_razon", None) and frag.startswith("Resultados de búsqueda")


def test_el_sin_de_una_marca_de_estado_no_corta_nombres_que_empiezan_con_sin():
    html = "<p>Resultados de busqueda: ALGO Sinopec ARGENTINA S.A. Sin Oferta Publica CUIT</p>"
    assert uni.razon_social_de_pagina(html)[1] == "ALGO Sinopec ARGENTINA S.A."


@pytest.mark.parametrize("csv,cnv,esperado", [
    ("PAMPA ENERGIA S.A.", "PAMPA ENERGIA S.A.", "coincide"),
    ("Pampa Energía", "PAMPA ENERGIA S.A.", "coincide"),                      # acentos, mayusculas y sufijos societarios no cuentan
    ("Edenor", "EMPRESA DISTRIBUIDORA Y COMERCIALIZADORA NORTE S.A. (EDENOR)", "coincide"),
    ("TRANSPORTADORA DE GAS DEL NORTE S.A.", "TRANSPORTADORA DE GAS DEL SUR S.A.", "parcial"),
    ("MIRGOR S.A.", "BANCO GALICIA", "distinta"),
    ("S.A.", "X", "distinta"),
])
def test_comparar_nombres(csv, cnv, esperado):
    assert uni.comparar(csv, cnv) == esperado


def test_consultar_reintenta_con_espera_creciente():
    esperas = []
    s = Sesion({C1: [ConnectionError("cae"), 503, OK]})
    r = uni.consultar_cuit(C1, s, intentos=3, espera=2, dormir=esperas.append)
    assert r["estado"] == "ok" and r["razon_social"] == "PAMPA ENERGIA S.A." and esperas == [2, 4]


def test_consultar_agota_reintentos_y_devuelve_error_sin_lanzar():
    r = uni.consultar_cuit(C1, Sesion({C1: [ConnectionError("a"), ConnectionError("b")]}), intentos=2, dormir=no_dormir)
    assert r["estado"] == "error" and "ConnectionError" in r["error"]


# --- verificar -------------------------------------------------------------------

FILAS = [
    {"empresa": "Pampa Energia S.A.", "cuit": C1},
    {"empresa": "Otra Empresa S.A.", "cuit": C2},       # la CNV dice otra cosa
    {"empresa": "Fantasma S.A.", "cuit": C3},           # portada generica
    {"empresa": "Caida S.A.", "cuit": C4},              # red caida
    {"empresa": "Sin Cuit S.A.", "cuit": ""},
    {"empresa": "Mal Digito S.A.", "cuit": C1[:-1] + str((int(C1[-1]) + 1) % 10)},
]
PAGINAS = {C1: OK, C2: OK, C3: PORTADA, C4: [ConnectionError("x")] * 3}


def test_verificar_clasifica_cada_fila_y_no_toca_el_csv():
    csv_antes = hashlib.sha256(open("data/universo.csv", "rb").read()).hexdigest()
    inf = uni.verificar(FILAS, Sesion(PAGINAS), dormir=no_dormir)
    v = {f["empresa"]: f["veredicto"] for f in inf["filas"]}
    assert v == {"Pampa Energia S.A.": "coincide", "Otra Empresa S.A.": "razon_social_distinta", "Fantasma S.A.": "cuit_no_existe_en_cnv",
                 "Caida S.A.": "error_de_red", "Sin Cuit S.A.": "sin_cuit", "Mal Digito S.A.": "cuit_invalido"}
    assert inf["resumen"]["coincide"] == 1 and inf["pendientes"] == 0
    assert hashlib.sha256(open("data/universo.csv", "rb").read()).hexdigest() == csv_antes


def test_verificar_no_consulta_cuit_invalido_ni_vacio():
    s = Sesion(PAGINAS)
    uni.verificar(FILAS, s, dormir=no_dormir)
    consultados = [u.split("/Empresa/")[1].split("?")[0] for u, _ in s.pedidos]
    assert sorted(set(consultados)) == sorted([C1, C2, C3, C4])


def test_verificar_retoma_desde_el_avance_y_reintenta_los_errores(tmp_path):
    ruta = str(tmp_path / "informes" / "u.json")
    s1 = Sesion(PAGINAS)
    uni.verificar(FILAS, s1, ruta_avance=ruta, dormir=no_dormir)
    assert os.path.exists(ruta) and not os.path.exists(ruta + ".tmp")
    s2 = Sesion({**PAGINAS, C4: OK})  # la red volvio
    inf = uni.verificar(FILAS, s2, ruta_avance=ruta, dormir=no_dormir)
    consultados = {u.split("/Empresa/")[1].split("?")[0] for u, _ in s2.pedidos}
    assert consultados == {C4}  # solo lo que habia fallado: lo ya verificado no se repite
    assert {f["empresa"]: f["veredicto"] for f in inf["filas"]}["Caida S.A."] == "razon_social_distinta"
    s3 = Sesion(PAGINAS)
    uni.verificar(FILAS, s3, ruta_avance=ruta, refrescar=True, dormir=no_dormir)
    assert len(s3.pedidos) >= 4


def test_verificar_con_tope_de_tiempo_deja_pendientes_y_guarda(tmp_path):
    ruta = str(tmp_path / "u.json")
    inf = uni.verificar(FILAS, Sesion(PAGINAS), ruta_avance=ruta, hasta_t=1.0, dormir=no_dormir)
    assert inf["pendientes"] == 4 and json.load(open(ruta))["pendientes"] == 4


def test_avance_ilegible_se_ignora(tmp_path):
    ruta = tmp_path / "u.json"
    ruta.write_text("{ esto no es json", encoding="utf-8")
    assert uni.verificar(FILAS[:1], Sesion(PAGINAS), ruta_avance=str(ruta), dormir=no_dormir)["resumen"] == {"coincide": 1}
    ruta.write_text('{"filas": "mal"}', encoding="utf-8")
    assert uni.verificar(FILAS[:1], Sesion(PAGINAS), ruta_avance=str(ruta), dormir=no_dormir)["resumen"] == {"coincide": 1}


def test_duplicados():
    filas = [{"empresa": "Pampa", "cuit": C1}, {"empresa": "PAMPA", "cuit": C1}]
    inf = uni.verificar(filas, Sesion({C1: OK}), dormir=no_dormir)
    assert inf["duplicados"] == [C1] and inf["nombres_repetidos"] == ["pampa"]


def test_markdown_de_la_verificacion():
    md = uni.a_markdown_verificacion(uni.verificar(FILAS, Sesion(PAGINAS), dormir=no_dormir))
    assert "la CNV dice «PAMPA ENERGIA S.A.»" in md and "**cuit_no_existe_en_cnv**: Fantasma S.A." in md and "El CSV no se modificó" in md
    assert "nada: todas las filas coinciden" in uni.a_markdown_verificacion(uni.verificar(FILAS[:1], Sesion(PAGINAS), dormir=no_dormir))
    pend = uni.verificar(FILAS, Sesion(PAGINAS), hasta_t=1.0, dormir=no_dormir)
    assert "pendientes" in uni.a_markdown_verificacion(pend)
    dup = uni.verificar([{"empresa": "Pampa", "cuit": C1}, {"empresa": "Pampa", "cuit": C1}], Sesion({C1: OK}), dormir=no_dormir)
    assert "duplicados" in uni.a_markdown_verificacion(dup) and "repetidas" in uni.a_markdown_verificacion(dup)


# --- sugerir -----------------------------------------------------------------------

AUTO = {
    "Transportadora de Gas del Sur": [],
    "transportadora gas sur": [{"cuit": "30-65786305-5", "descripcion": "TRANSPORTADORA DE GAS DEL SUR S.A."}, {"cuit": "30657863056", "descripcion": "TRANSPORTADORA DE GAS DEL NORTE S.A."}],
    "transportadora": [{"cuit": "30657863056", "descripcion": "TRANSPORTADORA DE GAS DEL NORTE S.A."}, {"cuit": "no", "descripcion": "basura"}],
}


def test_sugerir_junta_candidatos_sin_duplicar_y_marca_los_conocidos():
    universo = [{"empresa": "TGN", "cuit": "30657863056"}]
    r = uni.sugerir("Transportadora de Gas del Sur", universo, Sesion(auto=AUTO), buscar=lambda t, s: uni.buscar_autocompletado(t, s, dormir=no_dormir), dormir=no_dormir)
    assert [c["cuit"] for c in r["candidatos"]] == ["30657863055", "30657863056"] or len(r["candidatos"]) == 2
    por_cuit = {c["cuit"]: c for c in r["candidatos"]}
    assert por_cuit["30657863056"]["ya_esta_en_universo"] == "TGN"
    assert r["candidatos"][0]["parecido"] == "coincide"  # los que se parecen van primero
    assert "basura" not in json.dumps(r) and r["terminos_probados"][0] == "Transportadora de Gas del Sur"


def test_sugerir_tolera_errores_y_sin_resultados():
    def buscar(t, s):
        raise ConnectionError("cae")
    r = uni.sugerir("Algo", [], buscar=buscar, dormir=no_dormir)
    assert r["candidatos"] == [] and r["errores"] and "ConnectionError" in r["errores"][0]
    assert "ninguno" in uni.a_markdown_sugerencias(r) and "⚠" in uni.a_markdown_sugerencias(r)


def test_sugerir_confirma_con_la_pagina_de_cada_candidato():
    buscar = lambda t, s: [{"cuit": C1, "descripcion": "PAMPA ENERGIA S.A."}, {"cuit": C3, "descripcion": "PAMPA ALGO"}]  # noqa: E731
    r = uni.sugerir("Pampa", [], Sesion({C1: OK, C3: PORTADA}), buscar=buscar, confirmar=True, dormir=no_dormir)
    assert {c["cuit"]: c["pagina_cnv"] for c in r["candidatos"]} == {C1: "PAMPA ENERGIA S.A.", C3: "sin_marcador"}
    assert "página CNV: PAMPA ENERGIA S.A." in uni.a_markdown_sugerencias(r)


def test_autocompletado_respuesta_inesperada_reintenta_y_falla():
    with pytest.raises(ValueError):
        uni.buscar_autocompletado("x", Sesion(auto={"x": {"no": "lista"}}), intentos=2, dormir=no_dormir)
    assert uni.buscar_autocompletado("x", Sesion(auto={"x": [{"cuit": "1"}]})) == [{"cuit": "1"}]


# --- linea de comandos ------------------------------------------------------------

def test_cli_universo_sin_opciones(capsys):
    assert cli.main(["universo"]) == 2


def test_cli_universo_verificar_y_sugerir(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(salud, "leer_universo_completo", lambda: FILAS[:3])
    monkeypatch.setattr(uni, "sesion", lambda: Sesion(PAGINAS, AUTO))
    monkeypatch.setattr(uni.time, "sleep", lambda t: None)
    assert cli.main(["universo", "--verificar", "--salida", str(tmp_path / "i.json"), "--sugerir", "transportadora"]) == 0
    salida = capsys.readouterr().out
    assert "Verificación del universo" in salida and "Candidatos para «transportadora»" in salida
    assert os.path.exists(tmp_path / "i.json")


# --- hallazgo con la CNV real: ventana de fechas, "No se encontrarón resultados" y equivalencias --------------

def test_la_verificacion_pide_siempre_una_ventana_amplia():
    s = Sesion({C1: OK})
    uni.consultar_cuit(C1, s, dormir=no_dormir)
    assert s.pedidos[0][0].endswith(f"/Empresa/{C1}?fdesde=01/01/2016")


def test_no_se_encontraron_resultados_es_cuit_inexistente_y_no_razon_ilegible():
    estado, razon, frag = uni.razon_social_de_pagina(SIN_RESULTADOS)
    assert (estado, razon) == ("sin_resultados", None) and "No se encontrar" in frag
    inf = uni.verificar([{"empresa": "Fantasma S.A.", "cuit": C3}], Sesion({C3: SIN_RESULTADOS}), dormir=no_dormir)
    assert inf["filas"][0]["veredicto"] == "cuit_no_existe_en_cnv" and inf["resumen"] == {"cuit_no_existe_en_cnv": 1}
    # con o sin tilde en "encontrarón"
    assert uni.razon_social_de_pagina("<p>Resultados de busqueda: No se encontraron resultados</p>")[0] == "sin_resultados"


@pytest.mark.parametrize("csv,cnv,esperado", [
    ("Morixe Hnos", "MORIXE HERMANOS S.A.C.I.F.A.", "coincide"),
    ("Cia. Introductora de Buenos Aires", "COMPAÑIA INTRODUCTORA DE BUENOS AIRES S.A.", "coincide"),
    ("Importadora y Exportadora de la Patagonia", "IMP. Y EXP. DE LA PATAGONIA S.A.", "coincide"),
    ("Imp. y Exp. de la Patagonia SRL", "Importadora y Exportadora de la Patagonia S.R.L.", "coincide"),
    ("Empresa Ejemplo S.A.U.", "EMPRESA EJEMPLO S.A.", "coincide"),
    ("Edenor", "EMPRESA DISTRIBUIDORA Y COMERCIALIZADORA NORTE S.A. (EDENOR)", "coincide"),          # alias entre parentesis
    ("Empresa Distribuidora y Comercializadora Norte", "EMPRESA DISTRIBUIDORA Y COMERCIALIZADORA NORTE S.A. (EDENOR)", "coincide"),
    ("Transener", "Cia. De Transporte De Energia Electrica En Alta Tension S.A.", "distinta"),   # misma empresa pero solo una persona puede decirlo
    ("Banco Galicia", "BANCO MACRO S.A.", "parcial"),
])
def test_equivalencias_de_nombres(csv, cnv, esperado):
    assert uni.comparar(csv, cnv) == esperado


def test_transener_sigue_marcada_para_revision_humana():
    inf = uni.verificar([{"empresa": "Transener", "cuit": C1}], Sesion({C1: OK.replace("PAMPA ENERGIA S.A.", "Cia. De Transporte De Energia Electrica En Alta Tension S.A.")}), dormir=no_dormir)
    assert inf["filas"][0]["veredicto"] == "razon_social_distinta" and "A revisar a mano (1)" in uni.a_markdown_verificacion(inf)


def test_variantes_de_razon():
    assert uni.variantes_de_razon("A S.A. (ALIAS) y (OTRO)") == ["A S.A. (ALIAS) y (OTRO)", "A S.A. y", "ALIAS", "OTRO"]
    assert uni.variantes_de_razon("") == [] and uni.variantes_de_razon(None) == []
