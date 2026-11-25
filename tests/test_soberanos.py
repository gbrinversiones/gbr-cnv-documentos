import datetime as dt

from cnv import soberanos as so

RESULTADOS = """
<h5 class="seccion-rubro text-white bg-primary">RESOLUCIONES CONJUNTAS                                     </h5>
<div class="row"><div class="col-md-12">
<a href="/detalleAviso/primera/320419/20250131?busqueda=1" onclick="x">
<div class="linea-aviso">
<p class="item"><SPAN STYLE='BACKGROUND-COLOR: YELLOW;'>SECRETARÍA</SPAN> DE <SPAN>FINANZAS</SPAN> </p>
<p class="item-detalle"><small>Resolución Conjunta 6/2025</small></p>
<p class="item-detalle"><small>Fecha de Publicacion: 31/01/2025</small></p>
<p class="item-detalle" ><small>RESFC-2025-6-APN-SH#MEC<br/>la resolución conjunta 9 del 24 de enero</small></p>
</div></a></div></div>
<h5 class="seccion-rubro text-white bg-primary">RESOLUCIONES</h5>
<a href="/detalleAviso/primera/320500/20250131?busqueda=1"><div class="linea-aviso">
<p class="item">MINISTERIO DE ECONOMÍA</p><p class="item-detalle"><small>Resolución 100/2025</small></p></div></a>
"""


def test_parsear_resultados_y_filtro():
    avisos = so.parsear_resultados(RESULTADOS)
    assert [a["id"] for a in avisos] == ["320419", "320500"]
    a = avisos[0]
    assert a["fecha"] == "2025-01-31" and a["norma"] == "Resolución Conjunta 6/2025" and a["rubro"] == "RESOLUCIONES CONJUNTAS"
    assert a["codigo"] == "RESFC-2025-6-APN-SH#MEC"
    assert a["url"].endswith("/detalleAviso/primera/320419/20250131") and a["pdf"].endswith("/pdf/aviso/primera/320419/20250131")
    assert so.es_de_deuda(avisos[0]) and not so.es_de_deuda(avisos[1]) and so.es_de_deuda(avisos[1], ministerio=True)


def test_clases():
    assert so.clase_de("Letra del Tesoro Nacional capitalizable en pesos con vencimiento 30 de noviembre de 2026") == "lecap"
    assert so.clase_de("“Bono del Tesoro Nacional capitalizable en pesos con vencimiento 15 de enero de 2027”") == "boncap"
    assert so.clase_de("Bono del Tesoro Nacional en pesos cero cupón con ajuste por CER vencimiento 30 de junio de 2026") == "cer"
    assert so.clase_de("Letra del Tesoro Nacional vinculada al dólar estadounidense cero cupón") == "dolar_linked"
    assert so.clase_de("Bonos duales TAMAR / tasa fija") == "dual"
    assert so.clase_de("Bonos de la República Argentina en dólares estadounidenses Step Up 2030") == "usd"


ESPECIES = {
    "T15E7": {"clase": "boncap", "vencimiento": "2027-01-15"},
    "S30N6": {"clase": "lecap", "vencimiento": "2026-11-30"},
    "D30N6": {"clase": "dolar_linked", "vencimiento": "2026-11-30"},
}


def test_vincular_por_especie_o_por_vencimiento():
    assert so.vincular({"denominacion": "Bono capitalizable", "especie": "T15E7", "vencimiento": "2027-01-15"}, ESPECIES) == "T15E7"
    # la especie que dice el modelo no coincide con el vencimiento: se ignora y se vincula por clase y fecha
    assert so.vincular({"denominacion": "Letra del Tesoro capitalizable", "especie": "T15E7", "vencimiento": "2026-11-30"}, ESPECIES) == "S30N6"
    assert so.vincular({"denominacion": "Letra vinculada al dólar", "especie": None, "vencimiento": "2026-11-30"}, ESPECIES) == "D30N6"
    assert so.vincular({"denominacion": "Letra vinculada al dólar", "especie": None, "vencimiento": None}, ESPECIES) is None


def test_interpretar_descarta_basura():
    salida = 'Aqui va: {"instrumentos": [{"denominacion": "Letra X", "especie": "s30n6", "vencimiento": "2026-11-30", "moneda": "ARS", "accion": "emision", "articulo": 1}, {"denominacion": ""}, {"denominacion": "Y", "vencimiento": "30/11/2026", "accion": "rara"}]}'
    ins = so.interpretar(salida)
    assert len(ins) == 2 and ins[0]["especie"] == "S30N6" and ins[0]["articulo"] == "1"
    assert ins[1]["vencimiento"] is None and ins[1]["accion"] == "otra"
    assert so.interpretar("no hay json") == []


def test_construir_catalogo_emision_primero():
    normas = {
        "2": {"norma": "Resolución Conjunta 33/2026", "organismo": "SH", "fecha": "2026-06-17", "url": "u2", "pdf": "p2",
              "instrumentos": [{"denominacion": "Bono capitalizable 15/1/2027", "especie": "T15E7", "vencimiento": "2027-01-15", "accion": "ampliacion"}]},
        "1": {"norma": "Resolución Conjunta 6/2025", "organismo": "SH", "fecha": "2025-01-31", "url": "u1", "pdf": "p1",
              "instrumentos": [{"denominacion": "Bono capitalizable 15/1/2027", "especie": None, "vencimiento": "2027-01-15", "accion": "emision"}]},
        "3": {"norma": "Resolución Conjunta 40/2026", "organismo": "SH", "fecha": "2026-08-01", "url": "u3", "pdf": "p3",
              "instrumentos": [{"denominacion": "Bono capitalizable 15/1/2027", "especie": "T15E7", "vencimiento": "2027-01-15", "accion": "ampliacion"}]},
    }
    extras = [{"id": "sec-x", "especies": "GD30", "titulo": "Prospecto", "organismo": "SEC", "fecha": "2020-08-18", "url": "https://sec", "accion": "prospecto"}]
    c = so.construir_catalogo(normas, ESPECIES, extras, dt.datetime(2026, 11, 25, tzinfo=dt.timezone.utc))
    docs = c["titulos"]["T15E7"]["documentos"]
    assert [d["id"] for d in docs] == ["bo-1", "bo-3", "bo-2"]
    assert c["titulos"]["T15E7"]["clase"] == "boncap" and c["titulos"]["GD30"]["documentos"][0]["fuente"] == "otra"
    assert c["normas"]["1"]["instrumentos"][0]["vinculo"] == "T15E7"


def test_meses_cubre_el_rango():
    m = list(so.meses(dt.date(2025, 12, 1), dt.date(2026, 2, 10)))
    assert m == [(dt.date(2025, 12, 1), dt.date(2025, 12, 31)), (dt.date(2026, 1, 1), dt.date(2026, 1, 31)), (dt.date(2026, 2, 1), dt.date(2026, 2, 10))]


def test_parte_resolutiva():
    assert so.parte_resolutiva("Visto ... CONSIDERANDO ... RESUELVEN: ARTÍCULO 1º.- Dispónese").startswith("RESUELVEN")


class _R:
    def __init__(self, status, headers=None):
        self.status_code, self.headers = status, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Sesion:
    def __init__(self, estados):
        self.estados = list(estados)

    def get(self, url, **kw):
        return self.estados.pop(0)


def test_pedir_espera_ante_429_y_corta_si_sigue():
    esperas = []
    r = so.pedir(_Sesion([_R(429, {"Retry-After": "5"}), _R(200)]), "get", "u", dormir=esperas.append)
    assert r.status_code == 200 and esperas == [5]
    try:
        so.pedir(_Sesion([_R(429)] * 4), "get", "u", dormir=esperas.append)
        raise AssertionError("tenia que cortar")
    except so.Limitado:
        pass


def test_resolucion_del_ministerio_con_espacios_duros():
    aviso = {"organismo": "MINISTERIO DE\xa0ECONOMÍA", "norma": "Resolución\xa0381/2020"}
    assert so.es_de_deuda(aviso, ministerio=True)
    assert not so.es_de_deuda(aviso)
    assert so.clave_busqueda("27.556", True) != "27.556"


def test_vencimiento_del_ticker():
    assert so.vencimiento_del_ticker("S29E7") == "2027-01-29"
    assert so.vencimiento_del_ticker("S26F7") == "2027-02-26"
    assert so.vencimiento_del_ticker("TZXD6") == "" and so.vencimiento_del_ticker("AL30") == ""
    assert so.vencimiento_del_ticker("S31B7") == ""
