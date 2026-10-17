"""Errores de CUENTA/API (sin saldo, clave invalida, limite sostenido): no cuentan intentos, abortan el paso y `reintentos --reset` repara los marcadores."""
import json

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import avisos_pago as avp
from cnv import clasificacion as cla
from cnv import concurrencia as conc
from cnv import condiciones as con
from cnv import eventos as eve
from cnv import financieros as fin
from cnv import reintentos as rei
from cnv import resumenes as res

from .test_concurrencia import BALANCE, EMP, ClienteRapido, ErrorApi, montar_hechos, resp_fin
from .test_eventos import DIV_OK

SALDO = "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API.'}}"


def sin_saldo(c):
    raise ErrorApi(400, mensaje=SALDO)


@pytest.mark.parametrize("error,cuenta", [
    (ErrorApi(400, mensaje=SALDO), True), (ErrorApi(401), True), (ErrorApi(402), True), (ErrorApi(403), True), (conc.ErrorDeCuenta("x"), True),
    (ErrorApi(400, mensaje="prompt is too long"), False), (ErrorApi(429), False), (ErrorApi(500), False), (ValueError("json invalido"), False)])
def test_es_error_de_cuenta(error, cuenta):
    assert conc.es_error_de_cuenta(error) is cuenta


def test_un_limite_de_tasa_que_aguanta_todos_los_reintentos_es_de_cuenta(monkeypatch):
    monkeypatch.setattr(conc.time, "sleep", lambda s: None)
    cliente = ClienteRapido(lambda c: (_ for _ in ()).throw(ErrorApi(429, retry_after="0")))
    with pytest.raises(conc.ErrorDeCuenta):
        conc.crear_mensaje(cliente, model="m", system="s", messages=[{"role": "user", "content": "x"}])
    assert cliente.llamadas == conc.INTENTOS_API


@pytest.mark.parametrize("n_hilos", [1, 4])
def test_el_error_de_saldo_aborta_el_lote_sin_confirmar_nada(n_hilos, capsys):
    confirmados = []
    pend = [(EMP, {"id": str(i)}) for i in range(10)]

    def trabajar(e, m):
        raise ErrorApi(401)

    r = conc.ejecutar_lote(pend, trabajar, lambda e, m, x: confirmados.append(m), 100, n_hilos=n_hilos)
    assert r["corte"] == "cuenta" and not confirmados
    assert capsys.readouterr().out.count(conc.AVISO_CUENTA) == 1


@pytest.mark.parametrize("n_hilos", [1, 3])
def test_los_seis_pasos_abortan_sin_marcar_nada(tmp_path, n_hilos, capsys):
    a = alm.AlmacenLocal(str(tmp_path))
    ind = []
    for i in range(6):
        ind.append({"id": str(100 + i), "t": "hechos", "f": f"2026-10-{1 + i:02d}", "d": f"hecho {100 + i}", "texto_ok": True, "k": "dividendos"})
        ind.append({"id": str(200 + i), "t": "estados_contables", "f": f"2026-09-{1 + i:02d}", "texto_ok": True, "d": "TIPO BALANCE: CONSOLIDADO"})
        ind.append({"id": str(300 + i), "t": "avisos_pago", "f": f"2026-09-{1 + i:02d}", "texto_ok": True})
        ind.append({"id": str(400 + i), "t": "prospectos", "f": f"2026-09-{1 + i:02d}", "texto_ok": True, "s": "Suplemento"})
    alm.guardar_indice_empresa(a, EMP["cuit"], ind)
    for m in ind:
        alm.guardar_texto(a, EMP["cuit"], m["id"], BALANCE if m["t"] == "estados_contables" else "[pagina 1]\n" + "dividendo en efectivo " * 40)
    antes = {n: a.leer(n) for n in (f"cnv/indice/{EMP['cuit']}.json",)}
    for modulo, nombre in ((fin, "financieros"), (avp, "avisos_pago"), (con, "condiciones"), (eve, "eventos")):
        cliente = ClienteRapido(sin_saldo)
        total = modulo.ejecutar(a, [EMP], cliente, 100, n_hilos=n_hilos)
        assert total.get("errores", 0) == 0, nombre
        crudo = a.leer(f"cnv/{nombre}/{EMP['cuit']}.json")
        marcas = json.loads(crudo)["procesados"].values() if crudo else []
        assert not [p for p in marcas if p["estado"] == "error"], nombre              # ningun marcador de error ni intento gastado
    for modulo in (res, cla):
        total = modulo.ejecutar(a, [EMP], ClienteRapido(sin_saldo), 100, n_hilos=n_hilos)
        assert total.get("errores", 0) == 0 and total.get("resumidos", 0) == 0 and total.get("clasificados", 0) == 0
    assert a.leer(f"cnv/indice/{EMP['cuit']}.json") == antes[f"cnv/indice/{EMP['cuit']}.json"]
    assert capsys.readouterr().out.count(conc.AVISO_CUENTA) >= 4          # avisos y condiciones saltean estos textos sin llamar al modelo


def test_un_error_de_contenido_sigue_contando_intentos(tmp_path):
    a = montar_hechos(tmp_path, n=3, k="dividendos")
    cliente = ClienteRapido(lambda c: "esto no es json")
    t = eve.ejecutar(a, [EMP], cliente, 100, n_hilos=1)
    marcas = eve.leer_eventos(a, EMP["cuit"])["procesados"]
    assert t["errores"] == 3 and all(p["estado"] == "error" and p["intentos"] == 1 for p in marcas.values())


# --- reintentos --reset -------------------------------------------------------------------------

def sembrar(a):
    """Un marcador de error de API y uno de contenido en cada paso con marcadores."""
    for leer, guardar in ((fin.leer_financieros, fin.guardar_financieros), (avp.leer_avisos, avp.guardar_avisos),
                          (con.leer_condiciones, con.guardar_condiciones), (eve.leer_eventos, eve.guardar_eventos)):
        est = leer(a, EMP["cuit"])
        est["procesados"] = {
            "1": {"v": 1, "estado": "error", "intentos": 1, "error": "BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your cred"},
            "2": {"v": 1, "estado": "error", "intentos": 3, "error": "RateLimitError: Error code: 429 - rate limit"},
            "3": {"v": 1, "estado": "error", "intentos": 2, "error": "ValueError: JSON invalido en la respuesta"},
            "4": {"v": 1, "estado": "error", "intentos": 1, "error": "BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'prompt is too long'}}"},
            "5": {"v": 1, "estado": "ok"},
        }
        guardar(a, EMP["cuit"], est)


def test_es_error_de_api():
    for m in ("AuthenticationError: invalid x-api-key", "InternalServerError: Overloaded 529", "APIConnectionError: Connection error.", "PermissionDeniedError: x",
              "BadRequestError: Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low"):
        assert rei.es_error_de_api(m), m
    for m in ("", "ValueError: JSON invalido", "RuntimeError: cita no verificada", "BadRequestError: Error code: 400 - {'message': 'prompt is too long'}"):
        assert not rei.es_error_de_api(m), m


def test_reset_cubre_los_pasos_deja_los_errores_de_contenido_y_es_idempotente(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    sembrar(a)
    cuenta = rei.resetear(a, [EMP])
    assert cuenta == {"financieros": 2, "avisos-pago": 2, "condiciones": 2, "eventos": 2, "resumenes": 0, "clasificar": 0}
    for leer in (fin.leer_financieros, avp.leer_avisos, con.leer_condiciones, eve.leer_eventos):
        assert sorted(leer(a, EMP["cuit"])["procesados"]) == ["3", "4", "5"]
        assert leer(a, EMP["cuit"])["procesados"]["3"]["intentos"] == 2           # el error de contenido sigue contando
    despues = {n: a.leer(f"cnv/{n}/{EMP['cuit']}.json") for n in ("financieros", "avisos_pago", "condiciones", "eventos")}
    assert sum(rei.resetear(a, [EMP]).values()) == 0
    assert despues == {n: a.leer(f"cnv/{n}/{EMP['cuit']}.json") for n in despues}


def test_el_texto_del_reset_lista_los_seis_pasos(tmp_path):
    texto = rei.a_texto(rei.resetear(alm.AlmacenLocal(str(tmp_path)), [EMP]))
    for paso in ("financieros", "resumenes", "clasificar", "eventos", "condiciones", "avisos-pago"):
        assert f"{paso}: 0 documentos" in texto
