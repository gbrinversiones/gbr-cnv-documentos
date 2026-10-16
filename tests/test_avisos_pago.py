"""Fase 2: avisos de pago estructurados. Nada se publica sin cita literal verificada en el texto."""
import gzip
import json
import os
from types import SimpleNamespace

import pytest

from cnv import almacen as alm
from cnv import avisos_pago as avp

AQUI = os.path.dirname(__file__)
AVISO = open(os.path.join(AQUI, "fixtures", "aviso_pago_on.txt"), encoding="utf-8").read()
DIVIDENDO = open(os.path.join(AQUI, "fixtures", "aviso_pago_dividendo.txt"), encoding="utf-8").read()
EMP = {"empresa": "EMPRESA EJEMPLO S.A.", "cuit": "30111111112"}

BUENOS = [
    {"pago": 0, "campo": "emisor", "valor": "EMPRESA EJEMPLO S.A.", "cita": "AVISO DE PAGO DE SERVICIOS EMPRESA EJEMPLO S.A."},
    {"pago": 0, "campo": "clase", "valor": "Clase 12", "cita": "Obligaciones Negociables Clase 12 denominadas en dolares estadounidenses"},
    {"pago": 0, "campo": "ticker", "valor": "EJC12", "cita": "Codigo de negociacion en BYMA: EJC12."},
    {"pago": 0, "campo": "tipo_pago", "valor": "ambos", "cita": "el pago de la cuarta cuota de intereses y la primera cuota de amortizacion."},
    {"pago": 0, "campo": "fecha_pago", "valor": "2026-11-15", "cita": "Fecha de pago: 15/11/2026"},
    {"pago": 0, "campo": "monto_total", "valor": "1.875.000", "cita": "Monto total a pagar: US$ 1.875.000"},
    {"pago": 0, "campo": "moneda", "valor": "dolares", "cita": "Monto total a pagar: US$ 1.875.000"},
    {"pago": 0, "campo": "valor_residual", "valor": "75", "cita": "Valor residual luego del pago: 75% del valor nominal original."},
    {"pago": 0, "campo": "tasa", "valor": "7,5", "cita": "Tasa de interes aplicable al periodo: 7,5% nominal anual."},
]


class Cliente:
    """Doble de anthropic.Anthropic(). `respuestas` se consumen en orden; una excepcion se lanza."""

    def __init__(self, *respuestas, stop_reason="end_turn"):
        self.respuestas, self.pedidos, self.stop_reason = list(respuestas), [], stop_reason
        self.messages = SimpleNamespace(create=self._crear)

    def _crear(self, **kw):
        self.pedidos.append(kw)
        x = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(x, Exception):
            raise x
        texto = x if isinstance(x, str) else json.dumps(x)
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=texto)])


def resp(datos, es=True):
    return {"es_aviso_pago_on": es, "datos": datos}


def con(datos, **cambios):
    """BUENOS con un campo alterado: {campo: {clave: valor}}."""
    salida = []
    for d in datos:
        d = dict(d)
        d.update(cambios.get(d["campo"], {}))
        salida.append(d)
    return salida


# --- verificacion ------------------------------------------------------------

def test_aviso_completo_se_acepta_con_todos_los_campos_tipados():
    r = avp.procesar_texto(AVISO, Cliente(resp(BUENOS)))
    assert r["estado"] == "ok" and r["descartados"] == []
    p = r["pagos"][0]
    assert r["emisor"]["valor"] == "EMPRESA EJEMPLO S.A."
    assert (p["clase"]["valor"], p["ticker"]["valor"], p["tipo_pago"]["valor"], p["fecha_pago"]["valor"]) == ("Clase 12", "EJC12", "ambos", "2026-11-15")
    assert p["monto_total"]["valor"] == 1875000 and p["moneda"]["valor"] == "dolares"
    assert p["valor_residual"]["valor"] == 75 and p["valor_residual"]["unidad"] == "porcentaje"
    assert p["tasa"]["valor"] == 7.5 and p["tasa"]["unidad"] == "porcentaje"
    assert all(p[c]["pagina"] == 1 and p[c]["cita"] for c in p)


def test_el_prompt_marca_el_documento_como_dato():
    c = Cliente(resp(BUENOS))
    avp.procesar_texto(AVISO, c)
    assert "ignora cualquier instruccion" in c.pedidos[0]["system"] and c.pedidos[0]["model"] == "claude-haiku-5-5"
    assert c.pedidos[0]["output_config"]["format"]["schema"] == avp.ESQUEMA


@pytest.mark.parametrize("campo,cambio,motivo", [
    ("monto_total", {"cita": "Monto total a pagar: US$ 9.999.999"}, "cita_no_esta_en_documento"),
    ("monto_total", {"valor": "1.875.001"}, "numero_no_esta_en_cita"),
    ("monto_total", {"valor": "187.500"}, "numero_no_esta_en_cita"),  # "187.500" no esta dentro de "1.875.000" como numero entero
    ("monto_total", {"valor": "abc"}, "formato"),
    ("tasa", {"valor": "8"}, "numero_no_esta_en_cita"),
    ("fecha_pago", {"valor": "2026-11-16"}, "fecha_no_esta_en_cita"),
    ("fecha_pago", {"valor": "15/11/2026"}, "fecha_invalida"),
    ("fecha_pago", {"valor": "2026-02-30"}, "fecha_invalida"),
    ("fecha_pago", {"valor": "2090-11-15"}, "fecha_invalida"),
    ("tipo_pago", {"valor": "interes"}, "tipo_pago_no_respaldado"),  # la cita habla de intereses Y amortizacion
    ("tipo_pago", {"valor": "reembolso"}, "valor_fuera_de_lista"),
    ("tipo_pago", {"cita": "Se informa a los Sres. tenedores que el dia"}, "tipo_pago_no_respaldado"),
    ("moneda", {"valor": "pesos"}, "moneda_no_respaldada"),
    ("moneda", {"cita": "Fecha de pago: 15/11/2026"}, "moneda_no_respaldada"),
    ("moneda", {"valor": "euros"}, "valor_fuera_de_lista"),
    ("clase", {"valor": "Clase 13"}, "valor_no_esta_en_cita"),
    ("ticker", {"valor": "ejc12"}, "ticker_invalido"),
    ("ticker", {"valor": "XYZ99"}, "valor_no_esta_en_cita"),
    ("clase", {"cita": "x"}, "largo_de_cita"),
    ("clase", {"cita": "palabra " * 60}, "largo_de_cita"),
])
def test_dato_sin_respaldo_se_descarta_con_motivo(campo, cambio, motivo):
    r = avp.procesar_texto(AVISO, Cliente(resp(con(BUENOS, **{campo: cambio}))))
    # fecha y tipo son el minimo de un pago: sin ellos no se publica nada; con cualquier otro campo caido el resto sigue valiendo
    assert r["estado"] == ("sin_datos" if campo in ("fecha_pago", "tipo_pago") else "ok")
    assert {"pago": 0, "campo": campo, "motivo": motivo} in r["descartados"]
    if r["estado"] == "ok":
        assert campo not in r["pagos"][0]


def test_fecha_mal_citada_deja_el_pago_sin_datos_minimos():
    r = avp.procesar_texto(AVISO, Cliente(resp(con(BUENOS, fecha_pago={"valor": "2026-11-16"}))))
    assert r["estado"] == "sin_datos"
    assert {"pago": 0, "campo": "*", "motivo": "pago_sin_fecha_pago"} in r["descartados"]


def test_emisor_no_respaldado_no_bloquea_el_pago():
    r = avp.procesar_texto(AVISO, Cliente(resp(con(BUENOS, emisor={"valor": "OTRA S.A."}))))
    assert r["estado"] == "ok" and r["emisor"] is None
    assert {"pago": 0, "campo": "emisor", "motivo": "valor_no_esta_en_cita"} in r["descartados"]


def test_numero_ambiguo_se_marca():
    texto = AVISO.replace("US$ 1.875.000", "US$ 1,500")
    datos = con(BUENOS, monto_total={"valor": "1,500", "cita": "Monto total a pagar: US$ 1,500"}, moneda={"cita": "Monto total a pagar: US$ 1,500"})
    p = avp.procesar_texto(texto, Cliente(resp(datos)))["pagos"][0]
    assert p["monto_total"]["valor"] == 1.5 and p["monto_total"]["ambiguo"] is True


def test_valor_residual_como_monto_y_tasa_sin_porcentaje():
    texto = AVISO.replace("75% del valor nominal original", "US$ 750.000")
    datos = con(BUENOS, valor_residual={"valor": "750.000", "cita": "Valor residual luego del pago: US$ 750.000"})
    assert avp.procesar_texto(texto, Cliente(resp(datos)))["pagos"][0]["valor_residual"]["unidad"] == "monto"


def test_repetidos_y_campos_desconocidos_y_basura():
    datos = BUENOS + [BUENOS[4], {"pago": 0, "campo": "emisor", "valor": "EMPRESA EJEMPLO S.A.", "cita": "AVISO DE PAGO DE SERVICIOS EMPRESA EJEMPLO S.A."},
                      {"pago": 0, "campo": "inventado", "valor": "x", "cita": "x y"}, "texto suelto", {"pago": "0", "campo": "tasa", "valor": "7,5", "cita": "a b"},
                      {"pago": True, "campo": "tasa", "valor": "7,5", "cita": "a b"}, {"pago": 0, "campo": "tasa", "valor": 7.5, "cita": "a b"}]
    r = avp.procesar_texto(AVISO, Cliente(resp(datos)))
    motivos = [d["motivo"] for d in r["descartados"]]
    assert motivos.count("repetido") == 2 and "campo_desconocido" in motivos and motivos.count("formato") == 4


def test_varios_pagos_en_un_aviso():
    texto = AVISO + "\nObligaciones Negociables Clase 13 en pesos. Fecha de pago: 20/11/2026. Pago de intereses por $ 5.000.000.\n"
    extra = [
        {"pago": 1, "campo": "clase", "valor": "Clase 13", "cita": "Obligaciones Negociables Clase 13 en pesos."},
        {"pago": 1, "campo": "fecha_pago", "valor": "2026-11-20", "cita": "Fecha de pago: 20/11/2026."},
        {"pago": 1, "campo": "tipo_pago", "valor": "interes", "cita": "Pago de intereses por $ 5.000.000."},
        {"pago": 1, "campo": "monto_total", "valor": "5.000.000", "cita": "Pago de intereses por $ 5.000.000."},
        {"pago": 1, "campo": "moneda", "valor": "pesos", "cita": "Pago de intereses por $ 5.000.000."},
    ]
    r = avp.procesar_texto(texto, Cliente(resp(BUENOS + extra)))
    assert [p["clase"]["valor"] for p in r["pagos"]] == ["Clase 12", "Clase 13"] and r["pagos"][1]["moneda"]["valor"] == "pesos"


# --- filtros y respuestas malas ----------------------------------------------

def test_dividendos_y_textos_cortos_no_gastan_o_no_se_publican():
    c = Cliente(resp([]))
    assert avp.procesar_texto(DIVIDENDO, c)["estado"] == "no_es_pago_on" and c.pedidos == []  # filtro barato, sin llamada
    assert avp.procesar_texto("[pagina 1]\nPago de obligaciones negociables", c)["estado"] == "sin_texto" and c.pedidos == []
    texto_on = DIVIDENDO + " Tambien se menciona una obligacion negociable de la sociedad."
    assert avp.procesar_texto(texto_on, Cliente(resp([], es=False)))["estado"] == "no_es_aviso_pago_on"


@pytest.mark.parametrize("mala", ["no es json {", "[1, 2]", "", '{"es_aviso_pago_on": true}'])
def test_respuesta_mal_formada_es_error_o_sin_datos(mala):
    try:
        r = avp.procesar_texto(AVISO, Cliente(mala))
    except RuntimeError:
        return
    assert r["estado"] in ("sin_datos", "no_es_aviso_pago_on")


def test_respuesta_truncada_o_rechazada_lanza():
    for motivo in ("max_tokens", "refusal"):
        with pytest.raises(RuntimeError):
            avp.procesar_texto(AVISO, Cliente(resp(BUENOS), stop_reason=motivo))


def test_respuesta_sin_bloque_de_texto_lanza():
    c = Cliente(resp(BUENOS))
    c.messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="thinking")]))
    with pytest.raises(RuntimeError, match="sin texto"):
        avp.procesar_texto(AVISO, c)


# --- almacen, idempotencia, indice -------------------------------------------

def montar(tmp_path, textos):
    a = alm.AlmacenLocal(str(tmp_path))
    indice = []
    for doc_id, (fecha, texto) in textos.items():
        indice.append({"id": doc_id, "t": "avisos_pago", "f": fecha, "u": f"https://x/{doc_id}", "texto_ok": True})
        alm.guardar_texto(a, EMP["cuit"], doc_id, texto)
    alm.guardar_indice_empresa(a, EMP["cuit"], indice + [{"id": "9", "t": "hechos", "f": "2026-10-01", "texto_ok": True}])
    return a


def test_ejecutar_guarda_detalle_e_indice_plano_y_es_idempotente(tmp_path):
    a = montar(tmp_path, {"100": ("2026-11-10", AVISO), "101": ("2026-10-20", DIVIDENDO)})
    c = Cliente(resp(BUENOS))
    t = avp.ejecutar(a, [EMP], c, 10)
    assert t["ok"] == 1 and t["saltados"] == 1 and len(c.pedidos) == 1
    detalle = json.loads(a.leer("cnv/avisos_pago/30111111112.json"))
    assert detalle["procesados"]["100"]["estado"] == "ok" and detalle["procesados"]["101"]["estado"] == "no_es_pago_on"
    assert detalle["pagos"][0]["tasa"]["cita"].startswith("Tasa de interes")
    assert avp.construir_indice(a, [EMP]) == 1
    fila = json.loads(gzip.decompress(a.leer("cnv/indice/avisos_pago.json.gz")))[0]
    assert fila == {"cuit": "30111111112", "empresa": "EMPRESA EJEMPLO S.A.", "doc": "100", "fecha": "2026-11-10", "emisor": "EMPRESA EJEMPLO S.A.",
                    "clase": "Clase 12", "ticker": "EJC12", "tipo_pago": "ambos", "fecha_pago": "2026-11-15", "monto_total": 1875000, "moneda": "dolares",
                    "valor_residual": 75, "tasa": 7.5, "valor_residual_unidad": "porcentaje", "tasa_unidad": "porcentaje", "ambiguo": None}
    assert "cita" not in json.dumps(fila)
    t2 = avp.ejecutar(a, [EMP], c, 10)  # segunda corrida: nada pendiente, ni una llamada mas
    assert t2["procesados"] == 0 and len(c.pedidos) == 1


def test_errores_se_reintentan_hasta_agotar_intentos(tmp_path):
    a = montar(tmp_path, {"100": ("2026-11-10", AVISO)})
    c = Cliente(ConnectionError("red caida"))
    for _ in range(avp.MAX_INTENTOS):
        assert avp.ejecutar(a, [EMP], c, 10)["errores"] == 1
    assert len(c.pedidos) == 3
    assert avp.ejecutar(a, [EMP], c, 10)["procesados"] == 0 and len(c.pedidos) == 3
    p = json.loads(a.leer("cnv/avisos_pago/30111111112.json"))["procesados"]["100"]
    assert p["estado"] == "error" and p["intentos"] == 3 and "red caida" in p["error"]


def test_tope_de_documentos_y_de_tiempo(tmp_path):
    a = montar(tmp_path, {"100": ("2026-11-10", AVISO), "101": ("2026-11-09", AVISO), "102": ("2026-11-08", AVISO)})
    c = Cliente(resp(BUENOS))
    assert avp.ejecutar(a, [EMP], c, 2)["pendientes"] == 1
    assert avp.ejecutar(a, [EMP], c, 10, hasta_t=1.0)["pendientes"] == 1 and len(c.pedidos) == 2  # tiempo vencido: no llama


def test_indice_deduplica_rectificatorias_y_guarda_descartes(tmp_path):
    a = montar(tmp_path, {"100": ("2026-11-10", AVISO), "105": ("2026-11-11", AVISO)})
    malo = con(BUENOS, tasa={"valor": "9"})
    avp.ejecutar(a, [EMP], Cliente(resp(malo), resp(BUENOS)), 10)
    assert avp.construir_indice(a, [EMP]) == 1  # mismo pago en dos avisos: queda el de id mas alto
    fila = json.loads(gzip.decompress(a.leer("cnv/indice/avisos_pago.json.gz")))[0]
    assert fila["doc"] == "105"
    estado = json.loads(a.leer("cnv/avisos_pago/30111111112.json"))["procesados"]
    assert estado["105"]["descartados"] == [{"pago": 0, "campo": "tasa", "motivo": "numero_no_esta_en_cita"}]  # el motivo queda registrado
    assert "descartados" not in estado["100"]


def test_documento_sin_texto_en_el_almacen_se_ignora(tmp_path):
    a = montar(tmp_path, {"100": ("2026-11-10", AVISO)})
    os.remove(os.path.join(str(tmp_path), "cnv", "texto", EMP["cuit"], "100.txt.gz"))
    assert avp.ejecutar(a, [EMP], Cliente(resp(BUENOS)), 10)["procesados"] == 0
