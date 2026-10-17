"""Fase 21: eventos estructurados de hechos relevantes. Misma regla de cita literal verificada; modelo = doble de pruebas."""
import gzip
import json
from types import SimpleNamespace

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import eventos as eve

EMP = {"empresa": "EMPRESA EJEMPLO S.A.", "cuit": "30111111112"}

DIVIDENDOS = """[pagina 1]
EMPRESA EJEMPLO S.A. - HECHO RELEVANTE
Buenos Aires, 10 de octubre de 2026
El Directorio informa que la Asamblea aprobo el pago de un dividendo en efectivo por la suma total de $ 1.500.000.000 (pesos mil quinientos millones),
equivalente a $ 12,5 por accion. Los dividendos se pondran a disposicion de los accionistas el 20 de octubre de 2026 y tendran derecho a cobrarlos
quienes figuren inscriptos en el registro al 15/10/2026.
"""
ASAMBLEA = """[pagina 1]
CONVOCATORIA A ASAMBLEA GENERAL ORDINARIA Y EXTRAORDINARIA
Se convoca a los senores accionistas a la Asamblea General Ordinaria y Extraordinaria a celebrarse el 28 de noviembre de 2026 a las 11:00 horas.
ORDEN DEL DIA
1) Designacion de dos accionistas para firmar el acta.
2) Consideracion de la documentacion del articulo 234 de la Ley General de Sociedades.
3) Reforma del articulo cuarto del estatuto social.
"""
RECOMPRA = """[pagina 1]
El Directorio resolvio adquirir acciones propias hasta un monto maximo de $ 5.000.000.000 con un precio maximo de $ 340 por accion,
por un plazo de 120 dias corridos contados desde el dia habil siguiente a la publicacion.
"""
EMISION = """[pagina 1]
La Sociedad informa la emision de las Obligaciones Negociables Clase 9 por un valor nominal de US$ 30.000.000 a una tasa fija nominal anual del 8,25%.
"""
ADQUISICION = """[pagina 1]
La Sociedad suscribio un acuerdo para adquirir el 60% del capital social de Energia del Sur S.A. por un precio de US$ 45.000.000.
"""


def it(campo, valor, cita):
    return {"campo": campo, "valor": valor, "cita": cita}


C_TOTAL = "por la suma total de $ 1.500.000.000 (pesos mil quinientos millones)"
C_ACCION = "equivalente a $ 12,5 por accion"
C_PAGO = "se pondran a disposicion de los accionistas el 20 de octubre de 2026"
C_REG = "figuren inscriptos en el registro al 15/10/2026"
DIV_OK = [it("monto_total", "1.500.000.000", C_TOTAL), it("monto_por_accion", "12,5", C_ACCION), it("moneda", "pesos", C_TOTAL),
          it("fecha_pago", "2026-10-20", C_PAGO), it("fecha_registro", "2026-10-15", C_REG)]


class Cliente:
    def __init__(self, *respuestas):
        self.respuestas, self.pedidos = list(respuestas), []
        self.messages = SimpleNamespace(create=self._crear)

    def _crear(self, **kw):
        self.pedidos.append(kw)
        x = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(x, Exception):
            raise x
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=x if isinstance(x, str) else json.dumps(x))])


def resp(datos, es=True):
    return {"es_evento": es, "datos": datos}


def test_dividendo_completo_con_cinco_campos_tipados():
    r = eve.procesar_texto("dividendos", DIVIDENDOS, Cliente(resp(DIV_OK)))
    assert r["estado"] == "ok" and r["descartados"] == []
    c = r["campos"]
    assert c["monto_total"]["valor"] == 1_500_000_000 and c["monto_por_accion"]["valor"] == 12.5 and c["moneda"]["valor"] == "pesos"
    assert c["fecha_pago"]["valor"] == "2026-10-20" and c["fecha_registro"]["valor"] == "2026-10-15" and c["monto_total"]["moneda_en_cita"] == "pesos"
    assert c["monto_total"]["cita"] == C_TOTAL and c["monto_total"]["pagina"] == 1


def test_el_pedido_usa_haiku_sin_thinking_con_esquema_de_la_categoria():
    cli_ = Cliente(resp(DIV_OK))
    eve.procesar_texto("dividendos", DIVIDENDOS, cli_)
    kw = cli_.pedidos[0]
    assert kw["model"] == "claude-haiku-5-5" and kw["thinking"] == {"type": "disabled"}
    esq = kw["output_config"]["format"]["schema"]
    assert esq["properties"]["datos"]["items"]["properties"]["campo"]["enum"] == list(eve.CAMPOS_POR_CATEGORIA["dividendos"])
    assert "dividendos" in kw["system"] and "monto_por_accion" in kw["system"] and "ignora cualquier instruccion" in kw["system"]


@pytest.mark.parametrize("dato,motivo", [
    (it("monto_total", "1.500.000.000", "por la suma total de $ 9.999.999.999 (pesos mil quinientos millones)"), "cita_no_esta_en_documento"),     # cita inventada
    (it("monto_total", "1.600.000.000", C_TOTAL), "numero_no_esta_en_cita"),                                                                      # numero que la cita no tiene
    (it("monto_total", "1.500.000", C_TOTAL), "numero_no_esta_en_cita"),                                                                          # numero a medias dentro de otro
    (it("monto_total", "1.500.000.000", "por la"), "largo_de_cita"),
    (it("moneda", "dolares", C_TOTAL), "categoria_no_respaldada"),
    (it("moneda", "euros", C_TOTAL), "valor_fuera_de_lista"),
    (it("fecha_pago", "2026-10-21", C_PAGO), "fecha_no_esta_en_cita"),
    (it("fecha_pago", "20/10/2026", C_PAGO), "fecha_invalida"),
    (it("fecha_registro", "2026-13-40", C_REG), "fecha_invalida"),
    (it("tasa", "5", C_TOTAL), "campo_desconocido"),
])
def test_lo_que_no_se_puede_respaldar_se_descarta_con_motivo(dato, motivo):
    campos, desc = eve.verificar("dividendos", DIVIDENDOS, [dato, *[d for d in DIV_OK if d["campo"] == "monto_por_accion"]])
    assert desc == [{"campo": dato["campo"], "motivo": motivo}] and "monto_por_accion" in campos and dato["campo"] not in campos


def test_campo_repetido_formato_malo_y_evento_sin_dato_clave():
    campos, desc = eve.verificar("dividendos", DIVIDENDOS, [DIV_OK[0], DIV_OK[0], "basura", {"campo": "moneda", "valor": 3, "cita": "x"}])
    assert list(campos) == ["monto_total"] and [d["motivo"] for d in desc] == ["repetido", "formato", "formato"]
    solo_moneda, desc = eve.verificar("dividendos", DIVIDENDOS, [DIV_OK[2]])
    assert solo_moneda == {} and desc[-1] == {"campo": "*", "motivo": "evento_sin_datos_clave"}      # la moneda sola no es un evento


def test_asamblea_fecha_tipo_y_orden_del_dia_como_lista_de_citas():
    datos = [it("fecha", "2026-11-28", "a celebrarse el 28 de noviembre de 2026 a las 11:00 horas"),
             it("tipo_asamblea", "ordinaria_y_extraordinaria", "Asamblea General Ordinaria y Extraordinaria a celebrarse el 28 de noviembre"),
             it("orden_del_dia", "Designacion de dos accionistas para firmar el acta", "1) Designacion de dos accionistas para firmar el acta."),
             it("orden_del_dia", "Reforma del articulo cuarto del estatuto social", "3) Reforma del articulo cuarto del estatuto social."),
             it("orden_del_dia", "Aumento de capital a USD 10 millones", "4) Aumento de capital a USD 10 millones sujeto a aprobacion")]       # no esta en el texto
    r = eve.procesar_texto("convocatoria_asamblea", ASAMBLEA, Cliente(resp(datos)))
    c = r["campos"]
    assert r["estado"] == "ok" and c["fecha"]["valor"] == "2026-11-28" and c["tipo_asamblea"]["valor"] == "ordinaria_y_extraordinaria"
    assert [x["valor"] for x in c["orden_del_dia"]] == ["Designacion de dos accionistas para firmar el acta", "Reforma del articulo cuarto del estatuto social"]
    assert r["descartados"] == [{"campo": "orden_del_dia", "motivo": "cita_no_esta_en_documento"}]


@pytest.mark.parametrize("valor,cita,ok", [
    ("ordinaria", "Asamblea General Ordinaria a celebrarse el", True), ("ordinaria", "Asamblea General Extraordinaria a celebrarse el", False),
    ("extraordinaria", "Asamblea General Extraordinaria a celebrarse el", True), ("extraordinaria", "Asamblea General Ordinaria a celebrarse el", False),
    ("ordinaria_y_extraordinaria", "Asamblea General Ordinaria y Extraordinaria", True), ("ordinaria_y_extraordinaria", "Asamblea General Extraordinaria", False),
    ("ordinaria", "Asamblea General Ordinaria y Extraordinaria", False)])
def test_tipo_de_asamblea_distingue_ordinaria_de_extraordinaria(valor, cita, ok):
    assert eve._tipo_asamblea_respaldado(valor, eve.normalizar(cita)) is ok


def test_limite_de_puntos_del_orden_del_dia():
    texto = "[pagina 1]\n" + "\n".join(f"Punto numero {i} del orden del dia a considerar." for i in range(20))
    datos = [it("fecha", "2026-11-28", "28 de noviembre de 2026")] + [it("orden_del_dia", f"Punto numero {i} del orden del dia", f"Punto numero {i} del orden del dia a considerar.") for i in range(20)]
    campos, desc = eve.verificar("convocatoria_asamblea", texto + "\n28 de noviembre de 2026", datos)
    assert len(campos["orden_del_dia"]) == 12 and desc[-1]["motivo"] == "demasiados_puntos" and len(desc) == 8
    solo_lista, desc = eve.verificar("convocatoria_asamblea", texto, datos[1:3])
    assert solo_lista == {} and desc[-1]["motivo"] == "evento_sin_datos_clave"      # el orden del dia sin fecha no es un evento


def test_recompra_emision_y_adquisicion():
    rec = eve.procesar_texto("recompra_acciones", RECOMPRA, Cliente(resp([
        it("monto_maximo", "5.000.000.000", "hasta un monto maximo de $ 5.000.000.000 con un precio maximo de $ 340 por accion"),
        it("precio_maximo", "340", "con un precio maximo de $ 340 por accion,"), it("plazo", "120 dias corridos", "por un plazo de 120 dias corridos contados desde el dia habil siguiente")])))
    assert rec["estado"] == "ok" and rec["campos"]["precio_maximo"]["valor"] == 340 and rec["campos"]["plazo"]["valor"] == "120 dias corridos"
    em = eve.procesar_texto("emision_on", EMISION, Cliente(resp([
        it("clase", "Obligaciones Negociables Clase 9", "emision de las Obligaciones Negociables Clase 9 por un valor nominal"),
        it("monto", "30.000.000", "por un valor nominal de US$ 30.000.000 a una tasa fija"), it("moneda", "dolares", "por un valor nominal de US$ 30.000.000 a una tasa fija"),
        it("tasa", "8,25", "a una tasa fija nominal anual del 8,25%"), it("moneda", "uva", "emision de las Obligaciones Negociables Clase 9")])))
    assert em["campos"]["tasa"]["valor"] == 8.25 and em["campos"]["moneda"]["valor"] == "dolares" and em["campos"]["monto"]["moneda_en_cita"] == "dolares"
    assert em["descartados"] == [{"campo": "moneda", "motivo": "repetido"}]
    ad = eve.procesar_texto("adquisicion_venta", ADQUISICION, Cliente(resp([
        it("contraparte", "Energia del Sur S.A.", "adquirir el 60% del capital social de Energia del Sur S.A. por un precio"),
        it("porcentaje", "60", "adquirir el 60% del capital social de Energia del Sur S.A. por un precio"), it("monto", "45.000.000", "por un precio de US$ 45.000.000."),
        it("porcentaje", "60", "por un precio de US$ 45.000.000.")])))
    assert ad["campos"]["porcentaje"]["valor"] == 60 and ad["campos"]["contraparte"]["valor"] == "Energia del Sur S.A."


def test_porcentaje_exige_el_signo_en_la_cita():
    campos, desc = eve.verificar("adquisicion_venta", ADQUISICION, [it("porcentaje", "45", "por un precio de US$ 45.000.000.")])
    assert campos == {} and desc[0] == {"campo": "porcentaje", "motivo": "numero_no_esta_en_cita"}
    texto = "[pagina 1]\nLa Sociedad adquirio acciones representativas de 60 por ciento del capital de Alfa S.A.\n"
    ok, _ = eve.verificar("adquisicion_venta", texto, [it("porcentaje", "60", "adquirio acciones representativas de 60 por ciento del capital de Alfa S.A.")])
    assert ok["porcentaje"]["valor"] == 60
    sin_signo = "[pagina 1]\nLa Sociedad adquirio 60 acciones del capital de Alfa S.A. en el dia de hoy.\n"
    campos, desc = eve.verificar("adquisicion_venta", sin_signo, [it("porcentaje", "60", "La Sociedad adquirio 60 acciones del capital de Alfa S.A. en")])
    assert campos == {} and desc[0]["motivo"] == "porcentaje_sin_signo_en_cita"


def test_estados_de_un_documento():
    assert eve.procesar_texto("dividendos", "[pagina 1]\n[pagina 2]\n", Cliente(resp([])))["estado"] == "escaneado"
    assert eve.procesar_texto("dividendos", DIVIDENDOS, Cliente(resp([], es=False)))["estado"] == "no_es_evento"
    r = eve.procesar_texto("dividendos", DIVIDENDOS, Cliente(resp([it("monto_total", "1", "inventado inventado inventado")])))
    assert r["estado"] == "sin_datos" and r["descartados"] == [{"campo": "monto_total", "motivo": "cita_no_esta_en_documento"}]
    with pytest.raises(RuntimeError):
        eve.procesar_texto("dividendos", DIVIDENDOS, Cliente("no es json"))


def test_la_cita_se_verifica_contra_el_texto_completo_y_no_solo_lo_enviado():
    largo = "[pagina 1]\n" + "relleno " * 5000 + "\n[pagina 2]\n" + DIVIDENDOS.split("]\n", 1)[1]
    assert len(largo) > eve.MAX_CARACTERES
    cli_ = Cliente(resp([it("monto_total", "1.500.000.000", C_TOTAL)]))
    r = eve.procesar_texto("dividendos", largo, cli_)
    assert r["estado"] == "ok" and r["campos"]["monto_total"]["pagina"] == 2 and len(cli_.pedidos[0]["messages"][0]["content"]) < eve.MAX_CARACTERES + 200


# --- almacen, idempotencia, indice ---------------------------------------------------------------

def hecho(i, k, f="2026-10-10", **kw):
    return {"id": str(i), "t": "hechos", "f": f, "texto_ok": True, "d": f"hecho {i}", "u": f"http://x/{i}", "k": k, **kw}


@pytest.fixture
def base(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, EMP["cuit"], [hecho(1, "dividendos"), hecho(2, "convocatoria_asamblea", f="2026-10-12"), hecho(3, "resultados"), hecho(4, None),
                                                hecho(5, "dividendos", ocr=2), {"id": "6", "t": "prospectos", "f": "2026-10-01", "texto_ok": True, "k": "dividendos"},
                                                {"id": "7", "t": "hechos", "f": "2026-10-02", "texto_ok": False, "k": "dividendos"}])
    for i, t in ((1, DIVIDENDOS), (2, ASAMBLEA), (3, DIVIDENDOS), (5, DIVIDENDOS)):
        alm.guardar_texto(a, EMP["cuit"], str(i), t)
    return a


ASAMBLEA_OK = [it("fecha", "2026-11-28", "a celebrarse el 28 de noviembre de 2026 a las 11:00 horas"),
               it("orden_del_dia", "Reforma del articulo cuarto del estatuto social", "3) Reforma del articulo cuarto del estatuto social.")]


def test_candidatos_solo_hechos_clasificados_en_las_cinco_categorias_sin_ocr(base):
    assert [(m["id"], m["k"]) for _, m in eve.candidatos(base, [EMP])] == [("2", "convocatoria_asamblea"), ("1", "dividendos")]


def test_ejecutar_guarda_idempotente_y_respeta_el_tope(base):
    c = Cliente(resp(ASAMBLEA_OK), resp(DIV_OK))      # la asamblea es mas nueva: va primero
    t = eve.ejecutar(base, [EMP], c, max_docs=1)
    assert t == {"procesados": 1, "ok": 1, "sin_evento": 0, "saltados": 0, "errores": 0, "pendientes": 1} and len(c.pedidos) == 1
    t = eve.ejecutar(base, [EMP], Cliente(resp(DIV_OK)), max_docs=10)
    assert t["ok"] == 1 and t["pendientes"] == 0 and t["procesados"] == 1
    assert eve.ejecutar(base, [EMP], Cliente(resp(DIV_OK)), max_docs=10)["procesados"] == 0          # nada pendiente: ni una llamada
    estado = eve.leer_eventos(base, EMP["cuit"])
    assert {e["doc"] for e in estado["eventos"]} == {"1", "2"} and estado["procesados"]["1"]["v"] == "1" and estado["procesados"]["1"]["categoria"] == "dividendos"


def test_si_el_hecho_se_reclasifica_se_vuelve_a_extraer(base):
    eve.ejecutar(base, [EMP], Cliente(resp(ASAMBLEA_OK), resp(DIV_OK)), max_docs=10)
    ind = alm.leer_indice_empresa(base, EMP["cuit"])
    for m in ind:
        if m["id"] == "1":
            m["k"] = "recompra_acciones"
    alm.guardar_indice_empresa(base, EMP["cuit"], ind)
    assert [(m["id"], m["k"]) for _, m in eve.candidatos(base, [EMP])] == [("1", "recompra_acciones")]


def test_errores_se_reintentan_hasta_el_maximo_y_se_guarda_cada_documento(base):
    c = Cliente(RuntimeError("api caida"))
    for n in (1, 2, 3):
        t = eve.ejecutar(base, [EMP], c, max_docs=10)
        assert t["errores"] == 2, n
    assert eve.ejecutar(base, [EMP], c, max_docs=10)["procesados"] == 0                  # agotados los 3 intentos
    assert eve.leer_eventos(base, EMP["cuit"])["procesados"]["1"]["intentos"] == 3


def test_corte_por_tiempo_deja_el_resto_pendiente(base):
    t = eve.ejecutar(base, [EMP], Cliente(resp(DIV_OK)), max_docs=10, hasta_t=0.0001)
    assert t["procesados"] == 0 and t["pendientes"] == 2


def test_estado_sin_evento_y_escaneado_se_cuentan(base):
    alm.guardar_texto(base, EMP["cuit"], "2", "[pagina 1]\n")
    t = eve.ejecutar(base, [EMP], Cliente(resp([], es=False)), max_docs=10)
    assert t["saltados"] == 1 and t["sin_evento"] == 1 and t["ok"] == 0


def test_indice_ordenado_por_fecha_del_evento_con_doc_y_cita(base):
    eve.ejecutar(base, [EMP], Cliente(resp(ASAMBLEA_OK), resp(DIV_OK)), max_docs=10)
    assert eve.construir_indice(base, [EMP]) == 2
    d = json.loads(gzip.decompress(base.leer(eve.RUTA_INDICE)))
    assert d["version"] == 1 and [(f["doc"], f["categoria"], f["fecha"]) for f in d["filas"]] == [("2", "convocatoria_asamblea", "2026-11-28"), ("1", "dividendos", "2026-10-20")]
    div = d["filas"][1]
    assert div["datos"]["monto_total"] == 1_500_000_000 and div["citas"]["monto_total"] == {"cita": C_TOTAL, "pagina": 1} and div["url"] == "http://x/1" and div["fecha_presentacion"] == "2026-10-10"
    asamblea = d["filas"][0]
    assert asamblea["datos"]["orden_del_dia"] == ["Reforma del articulo cuarto del estatuto social"] and asamblea["citas"]["orden_del_dia"][0]["pagina"] == 1


def test_fecha_del_evento_cae_en_la_presentacion_si_no_hay_fecha_propia():
    base_ = {"fecha_presentacion": "2026-10-10"}
    assert eve.fecha_del_evento({**base_, "categoria": "dividendos", "campos": {"monto_total": {"valor": 1}}}) == "2026-10-10"
    assert eve.fecha_del_evento({**base_, "categoria": "dividendos", "campos": {"fecha_registro": {"valor": "2026-10-15"}}}) == "2026-10-15"
    assert eve.fecha_del_evento({**base_, "categoria": "emision_on", "campos": {}}) == "2026-10-10"
    assert eve.fecha_del_evento({**base_, "categoria": "convocatoria_asamblea", "campos": {}}) == "2026-10-10"


def test_indice_determinista_y_sin_reescribir(base):
    eve.ejecutar(base, [EMP], Cliente(resp(ASAMBLEA_OK), resp(DIV_OK)), max_docs=10)
    eve.construir_indice(base, [EMP])
    antes = base.leer(eve.RUTA_INDICE)
    assert eve.construir_indice(base, [EMP]) == 2 and base.leer(eve.RUTA_INDICE) == antes


def test_base_sin_eventos_deja_un_indice_vacio(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    assert eve.construir_indice(a, [EMP]) == 0 and json.loads(gzip.decompress(a.leer(eve.RUTA_INDICE))) == {"version": 1, "filas": []}


def test_cli_eventos(base, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cli.main(["eventos"]) == 2 and "falta ANTHROPIC_API_KEY" in capsys.readouterr().out
    assert cli.main(["eventos", "--solo-indice"]) == 0 and "indice eventos: 0 eventos" in capsys.readouterr().out
    fake = SimpleNamespace(Anthropic=lambda **k: Cliente(resp(ASAMBLEA_OK), resp(DIV_OK)))
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert cli.main(["eventos", "--max", "5", "--solo", EMP["cuit"]]) == 0
    salida = capsys.readouterr().out
    assert "'ok': 2" in salida and "indice eventos: 2 eventos" in salida
