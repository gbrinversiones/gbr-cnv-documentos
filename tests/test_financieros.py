import gzip
import json
import os
import time
from types import SimpleNamespace

import pytest

from cnv import almacen as alm
from cnv import financieros as fin

AQUI = os.path.dirname(__file__)
BALANCE = open(os.path.join(AQUI, "fixtures", "balance_sintetico.txt"), encoding="utf-8").read()

CITAS = {
    "ventas": "Ingresos por ventas 18.400.250 15.200.100 Costo de ventas (11.000.000) (9.500.000)",
    "resultado_operativo": "Gastos de comercializacion (2.100.000) (1.800.000) Resultado operativo 3.300.250 2.100.100",
    "resultado_neto": "Resultados financieros, netos (1.500.000) (1.200.000) Resultado del periodo 1.250.000 (350.500)",
    "activo_total": "Total del activo no corriente 25.123.457 24.234.568 Total del activo 35.000.000 33.000.000",
    "pasivo_total": "Total del pasivo 21.000.000 20.500.000 PATRIMONIO Total del patrimonio neto 14.000.000 12.500.000",
    "patrimonio_neto": "PATRIMONIO Total del patrimonio neto 14.000.000 12.500.000 Total del pasivo y patrimonio neto 35.000.000",
    "deuda_financiera": "Prestamos bancarios 7.200.800 6.600.400 Total de prestamos y obligaciones negociables 14.200.800 13.100.400",
    "efectivo": "Activo corriente Efectivo y equivalentes de efectivo 1.234.567 987.654 Cuentas por cobrar comerciales 4.500.000",
}
VALORES = {"ventas": 18400250, "resultado_operativo": 3300250, "resultado_neto": 1250000, "activo_total": 35000000,
           "pasivo_total": 21000000, "patrimonio_neto": 14000000, "deuda_financiera": 14200800, "efectivo": 1234567}


def respuesta(cifras=None, **extra):
    base = {"periodo": "2026-06-30", "tipo": "semestral", "acumulado": True, "moneda": "pesos", "unidad": "miles de pesos",
            "cifras": [{"campo": c, "valor": VALORES[c], "cita": CITAS[c]} for c in CAMPOS_ORDEN] if cifras is None else cifras}
    base.update(extra)
    return base


CAMPOS_ORDEN = list(fin.CAMPOS)


class ClienteFalso:
    """Doble de anthropic.Anthropic(): devuelve un JSON fijo y registra los pedidos. Nunca sale a la red."""

    def __init__(self, datos, stop_reason="end_turn"):
        self.datos, self.pedidos, self.stop_reason = datos, [], stop_reason
        self.messages = SimpleNamespace(create=self._crear)

    def _crear(self, **kw):
        self.pedidos.append(kw)
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=json.dumps(self.datos))])


# --- numeros argentinos ----------------------------------------------------

@pytest.mark.parametrize("token,esperado", [
    ("1.234.567,89", 1234567.89), ("1.234.567", 1234567), ("1.234", 1234), ("12,5", 12.5), ("0,75", 0.75),
    ("1.5", 1.5), ("1234", 1234), ("1,234,567", 1234567), ("1,234.56", 1234.56),
])
def test_a_numero_formato_argentino(token, esperado):
    assert fin.a_numero(token) == pytest.approx(esperado)


def test_a_numero_rechaza_basura():
    assert fin.a_numero("") is None and fin.a_numero("abc") is None and fin.a_numero("1.2.x") is None


def test_numeros_en_texto_negativos_y_fechas():
    # "350.500" es ambiguo (350500 en formato argentino, 350,5 en ingles): se aceptan las dos lecturas, ambas negativas
    leidos = fin.numeros_en_texto("Resultado (350.500) y -1.200,50")
    assert (350500, True) in leidos and (350.5, True) in leidos and (1200.5, True) in leidos
    assert fin.numeros_en_texto("Ganancia $ 1.250.000") == [(1250000, False)]
    assert fin.numeros_en_texto("al 30/06/2026 total 35.000.000") == [(35000000, False)]  # la fecha no cuenta como numero


# --- unidades ----------------------------------------------------------------

@pytest.mark.parametrize("unidad,factor", [
    ("miles de pesos", 1000), ("Miles de Pesos Argentinos", 1000), ("en miles de dólares estadounidenses", 1000),
    ("millones de dolares", 1_000_000), ("Millones de pesos", 1_000_000), ("pesos", 1), ("", 1),
])
def test_factor_unidad(unidad, factor):
    assert fin.factor_unidad(unidad) == factor


def test_valor_absoluto_respeta_la_unidad():
    ok, _ = fin.verificar_cifras(BALANCE, [{"campo": "ventas", "valor": 18400250, "cita": CITAS["ventas"]}], "miles de pesos")
    assert ok["ventas"]["valor"] == 18400250 and ok["ventas"]["valor_abs"] == 18400250 * 1000
    ok, _ = fin.verificar_cifras(BALANCE, [{"campo": "ventas", "valor": 18400250, "cita": CITAS["ventas"]}], "millones de dolares")
    assert ok["ventas"]["valor_abs"] == 18400250 * 1_000_000


# --- verificacion: la regla central -------------------------------------------

def test_cifra_con_cita_y_numero_verificables_se_conserva():
    ok, desc = fin.verificar_cifras(BALANCE, respuesta()["cifras"], "miles de pesos")
    assert set(ok) == set(fin.CAMPOS) and desc == []
    assert ok["ventas"]["pagina"] == 4 and ok["activo_total"]["pagina"] == 3
    assert ok["resultado_neto"]["valor"] == 1250000


def test_cita_ignora_acentos_mayusculas_y_saltos_de_linea():
    cita = "TOTAL DEL ACTIVO NO CORRIENTE 25.123.457\n24.234.568   total   del activo 35.000.000 33.000.000"
    ok, _ = fin.verificar_cifras(BALANCE, [{"campo": "activo_total", "valor": 35000000, "cita": cita}])
    assert "activo_total" in ok


def test_descarta_cifra_cuya_cita_no_esta_en_el_documento():
    inventada = "Total del activo consolidado 99.999.999 segun informe del directorio de la sociedad"
    ok, desc = fin.verificar_cifras(BALANCE, [{"campo": "activo_total", "valor": 99999999, "cita": inventada}])
    assert ok == {} and desc == [{"campo": "activo_total", "motivo": "cita_no_esta_en_documento"}]


def test_descarta_cifra_si_el_numero_no_esta_en_la_cita():
    # la cita es real pero el valor que dice el modelo no figura en ella
    ok, desc = fin.verificar_cifras(BALANCE, [{"campo": "ventas", "valor": 20000000, "cita": CITAS["ventas"]}])
    assert ok == {} and desc[0]["motivo"] == "numero_no_esta_en_cita"


def test_descarta_si_el_signo_no_coincide():
    # el documento muestra (350.500) pero el modelo lo da como positivo
    cita = "Resultados financieros, netos (1.500.000) (1.200.000) Resultado del periodo 1.250.000 (350.500)"
    ok, desc = fin.verificar_cifras(BALANCE, [{"campo": "resultado_neto", "valor": 350500, "cita": cita}])
    assert ok == {} and desc[0]["motivo"] == "numero_no_esta_en_cita"
    ok, _ = fin.verificar_cifras(BALANCE, [{"campo": "resultado_neto", "valor": -350500, "cita": cita}])
    assert ok["resultado_neto"]["valor"] == -350500


def test_descarta_citas_demasiado_cortas_o_largas_y_campos_raros():
    ok, desc = fin.verificar_cifras(BALANCE, [
        {"campo": "ventas", "valor": 18400250, "cita": "ventas 18.400.250"},  # 2 palabras: no alcanza
        {"campo": "activo_total", "valor": 35000000, "cita": " ".join(["Total del activo 35.000.000"] * 20)},  # demasiado larga
        {"campo": "ebitda", "valor": 1, "cita": CITAS["ventas"]},
    ])
    assert ok == {} and [d["motivo"] for d in desc] == ["largo_de_cita", "largo_de_cita", "campo_desconocido"]


def test_una_fila_corta_de_tabla_es_cita_valida():
    # caso real (Loma Negra, YPF): el rotulo y el numero ocupan una linea de 4 a 6 palabras
    texto = "[pagina 3]\nEstado de situacion financiera\nTotal del activo 35.000.000\nTotal del pasivo 20.000.000"
    ok, desc = fin.verificar_cifras(texto, [{"campo": "activo_total", "valor": 35000000, "cita": "Total del activo 35.000.000"}])
    assert ok["activo_total"]["valor"] == 35000000 and desc == []


def test_cifra_repetida_conserva_la_primera():
    c = respuesta()["cifras"][0]
    ok, desc = fin.verificar_cifras(BALANCE, [c, {**c, "valor": 1}])
    assert ok["ventas"]["valor"] == 18400250 and desc == [{"campo": "ventas", "motivo": "repetido"}]


# --- recorte -------------------------------------------------------------------

def test_recorte_elige_paginas_relevantes_y_respeta_el_tope():
    recorte, paginas = fin.armar_recorte(BALANCE)
    assert paginas == [1, 3, 4, 5]  # portada + balance + resultados + nota de prestamos; sin las notas de relleno
    assert "Hechos posteriores" not in recorte
    chico, _ = fin.armar_recorte(BALANCE, max_caracteres=2500)
    assert len(chico) <= 2500 + 200
    grande = "\n\n".join(f"[pagina {n}]\nEstado de situacion financiera total del activo " + "x " * 800 for n in range(1, 80))
    assert len(fin.armar_recorte(grande)[0]) <= fin.MAX_CARACTERES + 100


def test_recorte_vacio_si_no_hay_paginas_relevantes():
    assert fin.armar_recorte("[pagina 1]\nsolo texto general\n\n[pagina 2]\nmas texto general")[0] == ""


# --- documento escaneado ---------------------------------------------------------

def test_documento_escaneado_se_marca_y_no_llama_al_modelo():
    escaneado = "\n\n".join(f"[pagina {n}]\n" for n in range(1, 30))
    cliente = ClienteFalso(respuesta())
    assert fin.procesar_texto(escaneado, cliente) == {"estado": "escaneado"}
    assert fin.procesar_texto("", cliente) == {"estado": "escaneado"}
    assert cliente.pedidos == []


# --- llamada al modelo (siempre mockeada) -------------------------------------------

def test_procesar_texto_pide_haiku_sin_thinking_con_esquema_y_verifica():
    cliente = ClienteFalso(respuesta())
    r = fin.procesar_texto(BALANCE, cliente)
    (pedido,) = cliente.pedidos
    assert pedido["model"] == "claude-haiku-5-5" and pedido["thinking"] == {"type": "disabled"}
    assert pedido["output_config"]["format"]["type"] == "json_schema"
    assert "Hechos posteriores" not in pedido["messages"][0]["content"]
    assert r["estado"] == "ok" and r["periodo"] == "2026-06-30" and r["tipo"] == "semestral" and r["acumulado"] is True
    assert r["escala"] == 1000 and r["periodo_verificado"] and r["unidad_verificada"] and r["balance_coherente"] is True
    assert r["cifras"]["deuda_financiera"]["valor"] == 14200800


def test_procesar_texto_descarta_lo_no_respaldado():
    datos = respuesta()
    datos["cifras"][0]["valor"] = 1  # ventas: el numero no esta en la cita
    datos["cifras"][1]["cita"] = "Resultado operativo consolidado 3.300.250 segun el informe del directorio de la sociedad"  # cita inventada
    r = fin.procesar_texto(BALANCE, ClienteFalso(datos))
    assert "ventas" not in r["cifras"] and "resultado_operativo" not in r["cifras"]
    assert {d["motivo"] for d in r["descartadas"]} == {"numero_no_esta_en_cita", "cita_no_esta_en_documento"}


@pytest.mark.parametrize("extra,estado", [
    ({"periodo": "30/06/2026"}, "sin_periodo"), ({"periodo": "2031-01-01"}, "sin_periodo"),
    ({"tipo": "otro"}, "no_es_estado_contable"), ({"cifras": []}, "sin_cifras"),
])
def test_estados_de_descarte(extra, estado):
    assert fin.procesar_texto(BALANCE, ClienteFalso(respuesta(**extra)))["estado"] == estado


def test_respuesta_incompleta_del_modelo_es_error():
    with pytest.raises(RuntimeError):
        fin.extraer_con_claude("x", ClienteFalso(respuesta(), stop_reason="refusal"))


# --- almacen, idempotencia e indice -----------------------------------------------

EMP = {"empresa": "EMPRESA SINTETICA S.A.", "cuit": "30999999990"}


def preparar(tmp_path, docs=("3600000",), texto=BALANCE):
    a = alm.AlmacenLocal(str(tmp_path))
    metas = []
    for i in docs:
        alm.guardar_texto(a, EMP["cuit"], i, texto)
        metas.append({"id": i, "c": EMP["cuit"], "e": EMP["empresa"], "f": "2026-08-10", "t": "estados_contables", "u": f"https://x/{i}", "texto_ok": True})
    metas.append({"id": "1", "t": "hechos", "texto_ok": True, "f": "2026-08-11"})  # otro tipo: se ignora
    metas.append({"id": "2", "t": "estados_contables", "texto_ok": False, "f": "2026-08-12"})  # sin texto: se ignora
    alm.guardar_indice_empresa(a, EMP["cuit"], metas)
    return a


def test_idempotente_no_reprocesa_un_documento_ya_procesado(tmp_path):
    a = preparar(tmp_path)
    cliente = ClienteFalso(respuesta())
    t1 = fin.ejecutar(a, [EMP], cliente, 10)
    assert t1["ok"] == 1 and len(cliente.pedidos) == 1
    estado = fin.leer_financieros(a, EMP["cuit"])
    assert estado["procesados"]["3600000"]["v"] == fin.VERSION_EXTRACTOR and len(estado["periodos"]) == 1
    t2 = fin.ejecutar(a, [EMP], cliente, 10)
    assert t2["procesados"] == 0 and len(cliente.pedidos) == 1  # ni una llamada mas
    assert len(fin.leer_financieros(a, EMP["cuit"])["periodos"]) == 1


def test_nueva_version_del_extractor_reprocesa_y_reemplaza(tmp_path, monkeypatch):
    a = preparar(tmp_path)
    cliente = ClienteFalso(respuesta())
    fin.ejecutar(a, [EMP], cliente, 10)
    monkeypatch.setattr(fin, "VERSION_EXTRACTOR", "9")
    fin.ejecutar(a, [EMP], cliente, 10)
    estado = fin.leer_financieros(a, EMP["cuit"])
    assert len(cliente.pedidos) == 2 and len(estado["periodos"]) == 1 and estado["procesados"]["3600000"]["v"] == "9"


def test_escaneado_queda_marcado_y_no_se_reintenta(tmp_path):
    a = preparar(tmp_path, texto="\n\n".join(f"[pagina {n}]\n" for n in range(1, 20)))
    cliente = ClienteFalso(respuesta())
    t = fin.ejecutar(a, [EMP], cliente, 10)
    assert t["saltados"] == 1 and cliente.pedidos == []
    assert fin.leer_financieros(a, EMP["cuit"])["procesados"]["3600000"]["estado"] == "escaneado"
    assert fin.ejecutar(a, [EMP], cliente, 10)["procesados"] == 0


def test_error_de_api_se_reintenta_hasta_agotar_intentos(tmp_path):
    a = preparar(tmp_path)

    class Roto:
        messages = SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(ConnectionError("sin red")))

    for _ in range(fin.MAX_INTENTOS + 2):
        fin.ejecutar(a, [EMP], Roto(), 10)
    p = fin.leer_financieros(a, EMP["cuit"])["procesados"]["3600000"]
    assert p["estado"] == "error" and p["intentos"] == fin.MAX_INTENTOS
    assert fin.leer_financieros(a, EMP["cuit"])["periodos"] == []


def test_tope_de_documentos_y_los_mas_nuevos_primero(tmp_path):
    a = preparar(tmp_path, docs=("3600000", "3600001", "3600002"))
    cliente = ClienteFalso(respuesta())
    t = fin.ejecutar(a, [EMP], cliente, 2)
    assert len(cliente.pedidos) == 2 and t["pendientes"] == 1
    assert set(fin.leer_financieros(a, EMP["cuit"])["procesados"]) == {"3600002", "3600001"}


def test_corte_por_tiempo(tmp_path):
    a = preparar(tmp_path)
    cliente = ClienteFalso(respuesta())
    t = fin.ejecutar(a, [EMP], cliente, 10, hasta_t=time.monotonic() - 1)
    assert cliente.pedidos == [] and t["pendientes"] == 1


def test_indice_plano_deduplica_por_periodo_y_deja_el_mas_nuevo(tmp_path):
    a = preparar(tmp_path, docs=("3600000", "3600001"))
    fin.ejecutar(a, [EMP], ClienteFalso(respuesta()), 10)
    assert len(fin.leer_financieros(a, EMP["cuit"])["periodos"]) == 2  # el detalle por empresa guarda ambos documentos
    n = fin.construir_indice_financieros(a, [EMP])
    filas = json.loads(gzip.decompress(a.leer("cnv/indice/financieros.json.gz")))
    assert n == 1 and filas[0]["doc"] == "3600001"
    assert filas[0]["cuit"] == EMP["cuit"] and filas[0]["periodo"] == "2026-06-30" and filas[0]["ventas"] == 18400250
    assert filas[0]["escala"] == 1000 and filas[0]["efectivo"] == 1234567


def test_cli_financieros_sin_clave_no_llama_a_nada(monkeypatch, tmp_path, capsys):
    from cnv.__main__ import main

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    assert main(["financieros", "--max", "1"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


# --- aprendido con balances reales ---------------------------------------------

def test_balance_en_ingles_encuentra_las_paginas_relevantes():
    texto = "[pagina 1]\nYPF SOCIEDAD ANONIMA FINANCIAL STATEMENTS AS OF JUNE 30, 2026\n\n" + \
            "[pagina 2]\nNotas varias\n\n" + \
            "[pagina 3]\nCondensed interim consolidated statement of financial position\nTotal assets 100\nTotal liabilities 60\nTotal equity 40\nCash and cash equivalents 5\n\n" + \
            "[pagina 4]\nStatement of comprehensive income\nNet sales 200\nOperating profit 30\nNet income 10\n"
    recorte, paginas = fin.armar_recorte(texto)
    assert 3 in paginas and 4 in paginas and 2 not in paginas


def test_documento_de_una_vinculada_no_se_guarda_a_nombre_de_la_empresa():
    emp = {"empresa": "YPF S.A.", "cuit": "30546689979"}
    ajeno = {"d": "RELAC.: VINCULADA - CUIT: 30-71412830-9 - YPF ENERGIA ELECTRICA S.A. - ESTADOS FINANCIEROS"}
    propio = {"d": "CUIT: 30-54668997-9 - ESTADOS FINANCIEROS"}
    assert fin.entidad_ajena(ajeno, emp) is True
    assert fin.entidad_ajena({"d": "CUIT: 30-54668997-9 - X"}, {"empresa": "YPF", "cuit": "30546689979"}) is False
    assert fin.entidad_ajena({"d": "sin cuit en la descripcion"}, emp) is False
    assert propio["d"]


def test_consolidado_e_individual_no_se_pisan_en_el_indice(tmp_path):
    a = preparar(tmp_path)
    estado = fin.leer_financieros(a, EMP["cuit"])
    base = {"periodo": "2026-06-30", "tipo": "semestral", "acumulado": True, "moneda": "pesos", "unidad": "miles de pesos", "escala": 1000,
            "periodo_verificado": True, "unidad_verificada": True, "balance_coherente": None, "descartadas": []}
    cons = {"estado": "ok", **base, "cifras": {"ventas": {"valor": 200, "valor_abs": 200000, "cita": "x y z", "pagina": 1}}}
    indiv = {"estado": "ok", **base, "cifras": {"ventas": {"valor": 150, "valor_abs": 150000, "cita": "x y z", "pagina": 1}}}
    fin.registrar(estado, EMP, {"id": "10", "f": "2026-08-07", "u": "u", "d": "NORMA CONTABLE: NIIF - TIPO BALANCE: CONSOLIDADO - PERIODICIDAD: 3"}, cons)
    fin.registrar(estado, EMP, {"id": "11", "f": "2026-08-07", "u": "u", "d": "NORMA CONTABLE: NIIF - TIPO BALANCE: INDIVIDUAL - PERIODICIDAD: 3"}, indiv)
    fin.guardar_financieros(a, EMP["cuit"], estado)
    assert fin.construir_indice_financieros(a, [EMP]) == 2
    import gzip as _gz
    import json as _js
    filas = _js.loads(_gz.decompress(a.leer("cnv/indice/financieros.json.gz")))
    assert {f["balance"]: f["ventas"] for f in filas} == {"consolidado": 200, "individual": 150}


def test_numeros_en_formato_ingles_y_argentino():
    nl = chr(10)
    # balances en ingles (YPF): coma de miles y punto decimal
    ok, desc = fin.verificar_cifras("[pagina 1]" + nl + "Revenues 26 11,520 9,249 6,574 4,641", [{"campo": "ventas", "valor": 11520, "cita": "Revenues 26 11,520 9,249"}], "millones de dolares")
    assert ok["ventas"]["valor"] == 11520 and desc == []
    ok, _ = fin.verificar_cifras("[pagina 1]" + nl + "Total assets 1,234,567.89", [{"campo": "activo_total", "valor": 1234567.89, "cita": "Total assets 1,234,567.89"}])
    assert ok["activo_total"]["valor"] == 1234567.89
    # argentino: "11,52" sigue siendo 11,52 y "1.234.567" sigue siendo 1234567
    assert fin.numero_respaldado(11.52, "Cotizacion 11,52") and fin.numero_respaldado(1234567, "Ventas 1.234.567")
    # y un numero distinto no se respalda
    assert not fin.numero_respaldado(11521, "Revenues 11,520")
