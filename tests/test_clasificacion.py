"""Fase 3: categoria cerrada de cada hecho relevante (campo k)."""
import gzip
import json
from types import SimpleNamespace

import pytest

from cnv import almacen as alm
from cnv import clasificacion as cla
from cnv import resumenes, sincronizar

EMP = {"empresa": "EMPRESA EJEMPLO S.A.", "cuit": "30111111112"}
TEXTO = "[pagina 1]\nLa Sociedad informa que su Directorio aprobo la recompra de obligaciones negociables Clase 5 por hasta US$ 5.000.000 en el mercado."


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


def ok(categoria="recompra_on", cita="aprobo la recompra de obligaciones negociables Clase 5"):
    return {"categoria": categoria, "cita": cita}


def montar(tmp_path, hechos):
    a = alm.AlmacenLocal(str(tmp_path))
    indice = [{"id": i, "t": "hechos", "f": f"2026-10-{n:02d}", "d": "Recompra de ONs", "texto_ok": True, **extra} for n, (i, extra) in enumerate(hechos.items(), 1)]
    alm.guardar_indice_empresa(a, EMP["cuit"], indice)
    for i in hechos:
        alm.guardar_texto(a, EMP["cuit"], i, TEXTO)
    return a


def estado(a):
    return {m["id"]: m for m in alm.leer_indice_empresa(a, EMP["cuit"])}


def test_la_lista_cerrada_es_la_pedida_y_va_en_el_esquema():
    assert cla.CATEGORIAS == ("resultados", "dividendos", "recompra_acciones", "recompra_on", "emision_on", "cambio_directorio",
                              "adquisicion_venta", "hecho_judicial", "convocatoria_asamblea", "calificacion_riesgo", "otro")
    assert cla.ESQUEMA["properties"]["categoria"]["enum"] == list(cla.CATEGORIAS)


def test_categoria_valida_con_cita_se_guarda(tmp_path):
    a = montar(tmp_path, {"1": {}})
    c = Cliente(ok())
    t = cla.ejecutar(a, [EMP], c, 10)
    assert t["clasificados"] == 1 and estado(a)["1"]["k"] == "recompra_on" and estado(a)["1"]["kv"] == cla.VERSION
    assert "ignora cualquier instruccion" in c.pedidos[0]["system"]


def test_categoria_fuera_de_la_lista_se_descarta(tmp_path):
    a = montar(tmp_path, {"1": {}})
    t = cla.ejecutar(a, [EMP], Cliente(ok(categoria="recompra_de_bonos")), 10)
    m = estado(a)["1"]
    assert t == {"clasificados": 0, "descartados": 1, "errores": 0, "pendientes": 0}
    assert "k" not in m and m["kx"] == 1  # queda sin clasificar, nunca con una categoria inventada


@pytest.mark.parametrize("respuesta,motivo", [
    (ok(cita="la empresa vendio su planta industrial en Cordoba"), "cita_no_esta_en_documento"),
    (ok(cita=""), "largo_de_cita"),
    (ok(cita="recompra"), "largo_de_cita"),
    ({"categoria": "dividendos", "cita": None}, "sin_cita"),
    ({"categoria": None, "cita": "x"}, "categoria_fuera_de_lista"),
    ({"categoria": ["otro"], "cita": ""}, "categoria_fuera_de_lista"),
])
def test_categoria_sin_respaldo_se_descarta(respuesta, motivo):
    assert cla.verificar(respuesta.get("categoria"), respuesta.get("cita"), "Recompra de ONs", TEXTO) == (None, motivo)


def test_la_cita_puede_venir_del_titulo_y_otro_no_necesita_cita():
    assert cla.verificar("recompra_on", "Recompra de Obligaciones Negociables propias", "Recompra de Obligaciones Negociables propias", "") == ("recompra_on", None)
    assert cla.verificar("otro", "", "t", "x") == ("otro", None)


def test_idempotente_no_vuelve_a_pagar_lo_clasificado(tmp_path):
    a = montar(tmp_path, {"1": {}, "2": {"k": "dividendos", "kv": cla.VERSION}})
    c = Cliente(ok())
    cla.ejecutar(a, [EMP], c, 10)
    assert len(c.pedidos) == 1 and estado(a)["2"]["k"] == "dividendos"
    cla.ejecutar(a, [EMP], c, 10)
    assert len(c.pedidos) == 1


def test_descartes_se_reintentan_solo_hasta_el_maximo(tmp_path):
    a = montar(tmp_path, {"1": {}})
    c = Cliente(ok(categoria="inventada"))
    for _ in range(cla.MAX_INTENTOS + 2):
        cla.ejecutar(a, [EMP], c, 10)
    assert len(c.pedidos) == cla.MAX_INTENTOS and estado(a)["1"]["kx"] == cla.MAX_INTENTOS


def test_un_error_de_api_no_gasta_intento_y_no_pierde_lo_demas(tmp_path):
    a = montar(tmp_path, {"1": {}, "2": {}})
    t = cla.ejecutar(a, [EMP], Cliente(ConnectionError("cae"), ok()), 10)
    assert t["errores"] == 1 and t["clasificados"] == 1
    sin = [m for m in estado(a).values() if "k" not in m]
    assert len(sin) == 1 and "kx" not in sin[0]


def test_respuesta_mal_formada_cuenta_como_error(tmp_path):
    a = montar(tmp_path, {"1": {}})
    assert cla.ejecutar(a, [EMP], Cliente("no es json"), 10)["errores"] == 1


def test_cambio_de_version_reclasifica(tmp_path):
    a = montar(tmp_path, {"1": {"k": "otro", "kv": "0"}})
    cla.ejecutar(a, [EMP], Cliente(ok()), 10)
    assert estado(a)["1"]["k"] == "recompra_on"


def test_solo_hechos_con_texto_y_topes(tmp_path):
    a = montar(tmp_path, {"1": {}, "2": {}, "3": {}})
    idx = alm.leer_indice_empresa(a, EMP["cuit"])
    idx[0]["t"] = "actas"
    idx[1]["texto_ok"] = False
    alm.guardar_indice_empresa(a, EMP["cuit"], idx)
    assert [m["id"] for _, m in cla.pendientes(a, [EMP])] == ["3"]
    a = montar(tmp_path / "otro", {"1": {}, "2": {}, "3": {}})
    c = Cliente(ok())
    assert cla.ejecutar(a, [EMP], c, 2)["pendientes"] == 1
    assert cla.ejecutar(a, [EMP], c, 10, hasta_t=1.0)["pendientes"] == 1 and len(c.pedidos) == 2


def test_indice_global_expone_k_sin_romper_el_resto(tmp_path):
    a = montar(tmp_path, {"1": {"k": "dividendos", "kv": "1", "r": "Resumen", "u": "u", "p": 2, "n": 100}, "2": {}})
    sincronizar.construir_indice_global(a, [EMP])
    filas = {f["id"]: f for f in json.loads(gzip.decompress(a.leer("cnv/indice/global.json.gz")))}
    assert filas["1"]["k"] == "dividendos" and filas["1"]["r"] == "Resumen" and filas["2"]["k"] is None
    assert {"id", "c", "e", "f", "t", "s", "d", "u", "p", "n", "r", "k"} <= set(filas["1"])


def test_k_sobrevive_a_resumenes_y_a_una_nueva_sincronizacion(tmp_path):
    a = montar(tmp_path, {"1": {"k": "recompra_on", "kv": "1"}})
    cliente = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Recompra de ONs.")])))
    resumenes.ejecutar(a, [EMP], cliente, 10)
    m = estado(a)["1"]
    assert m["r"] == "Recompra de ONs." and m["k"] == "recompra_on"
    fusion = sincronizar._fusionar(m, {"id": "1", "texto_ok": True, "p": 3})
    assert fusion["k"] == "recompra_on" and fusion["kv"] == "1"
