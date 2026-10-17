"""Gasto real medido con response.usage y tope en dolares (CNV_MAX_USD)."""
from types import SimpleNamespace

import pytest

from cnv import concurrencia as conc


def _resp(ent=0, sal=0, leida=0, escrita=0):
    return SimpleNamespace(usage=SimpleNamespace(input_tokens=ent, output_tokens=sal, cache_read_input_tokens=leida, cache_creation_input_tokens=escrita))


@pytest.fixture(autouse=True)
def _limpio(monkeypatch):
    conc.reiniciar_gasto()
    monkeypatch.delenv("CNV_MAX_USD", raising=False)
    yield
    conc.reiniciar_gasto()


def test_precio_de_lista_prompt_corto():
    g = conc.Gasto()
    g.sumar(_resp(ent=50_000, sal=10_000))               # prompt <= 100K: 0,10 / 0,50 por millon
    assert g.usd == pytest.approx(50_000 * 0.10 / 1e6 + 10_000 * 0.50 / 1e6)
    assert (g.llamadas, g.entrada, g.salida) == (1, 50_000, 10_000)


def test_precio_de_lista_prompt_largo_y_cache():
    g = conc.Gasto()
    g.sumar(_resp(ent=200_000, sal=1_000))              # > 100K de prompt: 0,50 / 2,50
    assert g.usd == pytest.approx(200_000 * 0.50 / 1e6 + 1_000 * 2.50 / 1e6)
    c = conc.Gasto()
    c.sumar(_resp(ent=0, leida=1_000_000))               # prompt largo (1M leidos): entrada 0,50 y la lectura de cache vale el 10 % -> 0,05
    assert c.usd == pytest.approx(0.05)


def test_respuesta_sin_usage_no_rompe():
    g = conc.Gasto()
    g.sumar(SimpleNamespace())
    assert g.llamadas == 0 and g.usd == 0


def test_tope_no_numerico_o_cero_vale_el_defecto(monkeypatch):
    monkeypatch.setenv("CNV_MAX_USD", "abc")
    assert conc.tope_usd() == conc.TOPE_USD_DEFECTO
    monkeypatch.setenv("CNV_MAX_USD", "0")
    assert conc.tope_usd() == conc.TOPE_USD_DEFECTO
    monkeypatch.setenv("CNV_MAX_USD", "1.5")
    assert conc.tope_usd() == 1.5


def test_crear_mensaje_acumula_y_corta_al_llegar_al_tope(monkeypatch):
    monkeypatch.setenv("CNV_MAX_USD", "0.0002")
    monkeypatch.setattr(conc, "presupuesto", lambda: SimpleNamespace(esperar=lambda t: None))
    llamadas = []

    class Cliente:
        class messages:
            @staticmethod
            def create(**kw):
                llamadas.append(1)
                return _resp(ent=1_000, sal=100)        # 0,0001 + 0,00005 = 0,00015 por llamada

    conc.crear_mensaje(Cliente, model="m", messages=[{"role": "user", "content": "x"}])
    conc.crear_mensaje(Cliente, model="m", messages=[{"role": "user", "content": "x"}])
    with pytest.raises(conc.ErrorDeCuenta):               # ya se gasto 0,0003 >= tope 0,0002: no se llama mas
        conc.crear_mensaje(Cliente, model="m", messages=[{"role": "user", "content": "x"}])
    assert len(llamadas) == 2
    assert "USD~0.0003" in conc.gasto().resumen()
