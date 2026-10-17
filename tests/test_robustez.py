"""Fase 7: casos rotos. HTML roto, adjuntos vacios, ZIP corruptos, PDF cifrados, textos enormes, respuestas mal formadas del modelo,
GCS, y las ramas de la linea de comandos que ninguna otra prueba toca."""
import io
import json
import sys
import zipfile
from types import ModuleType, SimpleNamespace

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import avisos_pago as avp
from cnv import clasificacion as cla
from cnv import descarga, listado, resumenes, sincronizar
from cnv import financieros as fin

from .falsos import AnthropicFalso
from .pdfs import pdf_cifrado, pdf_con_paginas
from .test_financieros import BALANCE, CITAS, ClienteFalso, respuesta

EMP = {"empresa": "EMPRESA EJEMPLO S.A.", "cuit": "30111111112"}


# --- listado: HTML roto ----------------------------------------------------------------

@pytest.mark.parametrize("html", [
    "", "   ", "<html><body>sin tablas</body></html>", "<table class='tabla-hechos-relevantes'>", "<<<>>>&&&", "\x00\x01 basura binaria",
    '<h3>Hechos Relevantes</h3><table class="tabla-hechos-relevantes"><tr><td>solo una celda</td></tr></table>',
    '<h3>Hechos Relevantes</h3><table class="tabla-hechos-relevantes"><tr><td>14 oct. 2026</td><td></td><td>x</td><td>NO-NUMERICO</td><td><a href="https://x/publicview/a">v</a></td></tr></table>',
    '<h3>Hechos Relevantes</h3><table class="tabla-hechos-relevantes"><tr><td>14 oct. 2026</td><td></td><td>x</td><td>123</td><td><a href="https://x/otra/cosa">v</a></td></tr></table>',
    '<h3>Hechos Relevantes</h3><table class="tabla-hechos-relevantes"><tr><td>14 oct. 2026</td><td></td><td>x</td><td>123</td><td>sin link</td></tr></table>',
])
def test_listado_con_html_roto_devuelve_vacio_sin_lanzar(html):
    assert listado.parsear_listado(html) == []


def test_listado_tabla_sin_titulo_y_fila_rota_entre_filas_buenas():
    html = ('<table class="tabla-hechos-relevantes">'
            '<tr><td>1</td></tr>'
            '<tr><td>14 oct. 2026</td><td></td><td> Resultados  del\n trimestre </td><td>5</td><td><a href=" https://x/publicview/a ">v</a></td></tr>'
            '<tr><td>fecha rota</td><td></td><td>sin fecha</td><td>4</td><td><a href="https://x/publicview/b">v</a></td></tr></table>')
    docs = listado.parsear_listado(html)
    assert [d["doc_id"] for d in docs] == ["5", "4"] and docs[0]["descripcion"] == "Resultados del trimestre" and docs[0]["url"] == "https://x/publicview/a"
    assert docs[0]["tipo"] == "otros" and docs[1]["fecha"] == ""  # sin titulo de seccion -> "otros"; fecha ilegible no se inventa


def test_listado_la_seccion_que_tipifica_gana_sobre_otros():
    fila = '<tr><td>1 oct. 2026</td><td></td><td>t</td><td>7</td><td><a href="https://x/publicview/a">v</a></td></tr>'
    html = f'<h3>Ultima Informacion Recibida</h3><table class="tabla-hechos-relevantes">{fila}</table><h3>Hechos Relevantes</h3><table class="tabla-hechos-relevantes">{fila}</table>'
    docs = listado.parsear_listado(html)
    assert len(docs) == 1 and docs[0]["tipo"] == "hechos"


def test_sesion_y_listar():
    s = listado.sesion()
    assert s.verify is False and "Mozilla" in s.headers["User-Agent"]

    class Falsa:
        def get(self, url, timeout=None):
            self.url = url
            return SimpleNamespace(raise_for_status=lambda: None, text='<h3>Actas</h3><table class="tabla-hechos-relevantes"><tr><td>1 oct. 2026</td><td></td><td>a</td><td>9</td><td><a href="https://x/publicview/a">v</a></td></tr></table>', encoding=None)

    f = Falsa()
    import datetime as dt
    assert [d["tipo"] for d in listado.listar("30-11111111-2", dt.date(2026, 1, 5), f)] == ["actas"]
    assert f.url.endswith("/30111111112?fdesde=05/01/2026")

    class Cae:
        def get(self, url, timeout=None):
            return SimpleNamespace(raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError("503")), text="", encoding=None)

    with pytest.raises(RuntimeError):
        listado.listar("30111111112", dt.date(2026, 1, 5), Cae())


# --- adjuntos, ZIP y PDF -----------------------------------------------------------------

@pytest.mark.parametrize("html", [
    "", "<propiedad uploader='x'>no es json</propiedad>", '<propiedad uploader="x">[{"guid":</propiedad>', '<propiedad uploader="x">{"guid":"a"}</propiedad>',
    '<propiedad uploader="x">[1, "dos", null]</propiedad>', '<propiedad uploader="x">[{"guid":"a"}]</propiedad>', '<propiedad uploader="x">[]</propiedad>',
])
def test_adjuntos_con_basura(html):
    assert descarga.adjuntos(html) == []


def test_pdfs_de_zip_corrupto_vacio_y_con_basura():
    assert descarga.pdfs_de(b"PK\x03\x04" + b"\x00" * 50) == []                 # zip truncado
    assert descarga.pdfs_de(b"") == [] and descarga.pdfs_de(b"%PD") == []
    vacio = io.BytesIO()
    zipfile.ZipFile(vacio, "w").close()
    assert descarga.pdfs_de(vacio.getvalue()) == []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("leeme.txt", b"hola")
        z.writestr("CARPETA/Balance.PDF", pdf_con_paginas(["x"]))
    assert [n for n, _ in descarga.pdfs_de(buf.getvalue())] == ["CARPETA/Balance.PDF"]  # extension en mayusculas
    # zip con cifrado: leer lanza RuntimeError, no debe tirar la corrida
    cifrado = io.BytesIO()
    with zipfile.ZipFile(cifrado, "w") as z:
        z.writestr("a.pdf", b"%PDF-1.4 x")
    datos = bytearray(cifrado.getvalue())
    datos[6] |= 0x01  # bit "cifrado" en la cabecera local
    datos[datos.index(b"PK\x01\x02") + 8] |= 0x01  # y en el directorio central
    assert descarga.pdfs_de(bytes(datos)) == []


def test_texto_de_pdf_compatible_y_pdf_sin_paginas():
    assert descarga.texto_de_pdf(pdf_con_paginas(["a", "b"]), 1) == ("[pagina 1]\na", 2)
    r = descarga.extraer_pdf(pdf_con_paginas([]))
    assert r["texto"] == "" and r["paginas"] == 0 and r["truncado"] is None


def test_pdf_cifrado_con_clave_vacia_se_lee():
    from .pdfs import pdf_cifrado_clave_vacia

    assert "legible" in descarga.extraer_pdf(pdf_cifrado_clave_vacia(["texto legible con clave vacia"]))["texto"]


def test_texto_enorme_de_pdf_respeta_los_topes():
    pdf = pdf_con_paginas(["linea " * 300] * 60)  # ~1800 caracteres por pagina
    r = descarga.extraer_pdf(pdf, max_paginas=400, max_caracteres=20_000)
    assert len(r["texto"]) <= 20_000 and r["truncado"] == "caracteres" and 1 <= r["leidas"] < 60


def test_pagina_que_no_se_puede_extraer_no_tira_el_documento(monkeypatch):
    from pypdf import PageObject

    real = PageObject.extract_text
    llamadas = {"n": 0}

    def rota(self, *a, **k):
        llamadas["n"] += 1
        if llamadas["n"] == 2:
            raise ValueError("fuente rota")
        return real(self, *a, **k)

    monkeypatch.setattr(PageObject, "extract_text", rota)
    r = descarga.extraer_pdf(pdf_con_paginas(["uno", "dos", "tres"]))
    assert r["leidas"] == 3 and "[pagina 2]\n\n" in r["texto"] and "tres" in r["texto"]


def test_descarga_valet_sin_llave_y_json_roto_son_reintentables():
    class S:
        def get(self, url, timeout=None):
            return SimpleNamespace(status_code=200, json=lambda: {"otra": "cosa"})

    with pytest.raises(descarga.ErrorDescarga) as e:
        descarga.descargar_bytes("g", S(), intentos=1)
    assert e.value.reintentable and "KeyError" in e.value.motivo


def test_presentacion_adjunto_vacio_y_sin_tiempo(monkeypatch):
    from .test_tipos import Resp, SesionFalsa, pagina

    s = SesionFalsa(pagina(("a.pdf", "ga")), blobs={"ga": [Resp(200, b"x" * 10)]})
    assert descarga.texto_de_presentacion("u", s) == {"fallos": [{"archivo": "a.pdf", "motivo": "adjunto vacio"}]}
    s = SesionFalsa(pagina(("a.pdf", "ga")), blobs={"ga": [Resp(200, pdf_con_paginas(["x"]))]})
    r = descarga.texto_de_presentacion("u", s, hasta_t=1.0)  # tiempo ya vencido
    assert r == {"fallos": [{"archivo": "a.pdf", "motivo": "sin tiempo"}]} and s.llamadas == []


def test_presentacion_cupo_agotado_por_el_primer_adjunto():
    from .test_tipos import Resp, SesionFalsa, pagina

    pdf = pdf_con_paginas(["uno", "dos"])
    s = SesionFalsa(pagina(("a.pdf", "ga"), ("b.pdf", "gb")), blobs={"ga": [Resp(200, pdf)], "gb": [Resp(200, pdf)]})
    r = descarga.texto_de_presentacion("u", s, limites=descarga.Limites(paginas=2))
    assert r["archivos"] == ["a.pdf"] and r["truncado"]["motivo"] == "paginas"


# --- sincronizar -----------------------------------------------------------------------------

def test_con_reintentos_espera_entre_intentos_pero_no_despues_del_ultimo(monkeypatch):
    esperas = []
    monkeypatch.setattr(sincronizar.time, "sleep", esperas.append)
    with pytest.raises(KeyError):
        sincronizar._con_reintentos(lambda: (_ for _ in ()).throw(KeyError("x")), intentos=3, espera=2)
    assert esperas == [2, 4]
    assert sincronizar._con_reintentos(lambda: 5) == 5


def test_cargar_universo_ignora_filas_sin_cuit(tmp_path):
    ruta = tmp_path / "u.csv"
    ruta.write_text("empresa,cuit,origen\nA,30-54668997-9,BYMA\nB,,BYMA\n", encoding="utf-8")
    assert sincronizar.cargar_universo(str(ruta)) == [{"empresa": "A", "cuit": "30546689979"}]


def test_proceso_que_lanza_una_excepcion_queda_registrado_como_fallo(tmp_path, monkeypatch):
    a = alm.AlmacenLocal(str(tmp_path))
    monkeypatch.setattr(sincronizar.time, "sleep", lambda t: None)
    monkeypatch.setattr(sincronizar, "sesion", lambda: None)

    def cae(*a, **k):
        raise ConnectionError("la CNV corto")

    monkeypatch.setattr(sincronizar, "texto_de_presentacion", cae)
    d = {"doc_id": "5", "fecha": "2026-10-01", "seccion": "HR", "tipo": "hechos", "descripcion": "x" * 500, "url": "https://x/publicview/a"}
    meta = sincronizar._procesar(a, EMP, d, None)
    assert meta["texto_ok"] is False and meta["intentos"] == 1 and "la CNV corto" in meta["error"] and len(meta["d"]) == 300


def test_sin_adjunto_y_con_adjunto_sin_texto(tmp_path, monkeypatch):
    a = alm.AlmacenLocal(str(tmp_path))
    monkeypatch.setattr(sincronizar, "sesion", lambda: None)
    d = {"doc_id": "5", "fecha": "2026-10-01", "seccion": "HR", "tipo": "hechos", "descripcion": "x", "url": "u"}
    monkeypatch.setattr(sincronizar, "texto_de_presentacion", lambda *a, **k: {})
    assert sincronizar._procesar(a, EMP, d, None)["error"] == "sin adjunto"
    monkeypatch.setattr(sincronizar, "texto_de_presentacion", lambda *a, **k: {"texto": "  ", "escaneado": True, "ocr_intentado": True})
    m = sincronizar._procesar(a, EMP, d, None)
    assert m["error"] == "sin texto" and m["escaneado"] is True and m["ocr_intentado"] is True


def test_empresa_sin_documentos_nuevos_y_listado_que_cae(tmp_path, monkeypatch):
    a = alm.AlmacenLocal(str(tmp_path))
    monkeypatch.setattr(sincronizar.time, "sleep", lambda t: None)
    monkeypatch.setattr(sincronizar, "listar", lambda cuit, desde: [])
    import datetime as dt
    assert sincronizar.sincronizar_empresa(a, EMP, dt.date(2026, 1, 1)) == {"nuevos": 0, "fallidos": 0, "pendientes": 0}
    assert alm.leer_indice_empresa(a, EMP["cuit"]) == []

    def cae(cuit, desde):
        raise ConnectionError("sin red")

    monkeypatch.setattr(sincronizar, "listar", cae)
    with pytest.raises(ConnectionError):
        sincronizar.sincronizar_empresa(a, EMP, dt.date(2026, 1, 1))


def test_tope_de_tiempo_deja_el_resto_pendiente(tmp_path, monkeypatch):
    a = alm.AlmacenLocal(str(tmp_path))
    import datetime as dt
    docs = [{"doc_id": str(i), "fecha": "2026-10-01", "seccion": "HR", "tipo": "hechos", "descripcion": "x", "url": f"u{i}"} for i in (3, 2, 1)]
    monkeypatch.setattr(sincronizar, "listar", lambda cuit, desde: docs)
    monkeypatch.setattr(sincronizar, "texto_de_presentacion", lambda *a, **k: pytest.fail("no debia descargar"))
    r = sincronizar.sincronizar_empresa(a, EMP, dt.date(2026, 1, 1), hasta_t=1.0)
    assert r == {"nuevos": 0, "fallidos": 0, "pendientes": 3}
    r = sincronizar.sincronizar_empresa(a, EMP, dt.date(2026, 1, 1), max_docs=0)
    assert r["pendientes"] == 0


# --- almacen: GCS ---------------------------------------------------------------------------

class NotFound(Exception):
    """Como google.api_core.exceptions.NotFound: el codigo lo reconoce por el nombre de la clase."""


class BlobFalso:
    def __init__(self, tienda, nombre):
        self.tienda, self.nombre = tienda, nombre

    def upload_from_string(self, datos):
        self.tienda[self.nombre] = datos

    def exists(self):
        return self.nombre in self.tienda

    def download_as_bytes(self):
        if self.nombre not in self.tienda:
            raise NotFound(self.nombre)
        return self.tienda[self.nombre]


def instalar_gcs(monkeypatch):
    tienda, creado = {}, {}
    cliente = SimpleNamespace(bucket=lambda n: SimpleNamespace(blob=lambda nombre: BlobFalso(tienda, nombre)))
    storage = ModuleType("google.cloud.storage")
    storage.Client = lambda: creado.setdefault("modo", "defecto") and cliente
    storage.Client.from_service_account_info = lambda info: creado.update(modo="cuenta", info=info) or cliente
    cloud = ModuleType("google.cloud")
    cloud.storage = storage
    monkeypatch.setitem(sys.modules, "google", ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage)
    return tienda, creado


def test_almacen_gcs_ida_y_vuelta_con_credenciales(monkeypatch):
    tienda, creado = instalar_gcs(monkeypatch)
    a = alm.AlmacenGCS("mi-bucket", json.dumps({"type": "service_account", "project_id": "falso"}))
    assert creado["modo"] == "cuenta" and creado["info"]["project_id"] == "falso"
    a.guardar("cnv/x.json", b"{}")
    assert a.existe("cnv/x.json") and a.leer("cnv/x.json") == b"{}" and a.leer("cnv/nada") is None and not a.existe("cnv/nada")


def test_almacen_gcs_sin_credenciales_explicitas(monkeypatch):
    _, creado = instalar_gcs(monkeypatch)
    alm.AlmacenGCS("mi-bucket")
    assert creado["modo"] == "defecto"


def test_almacen_desde_entorno(monkeypatch, tmp_path):
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    a = alm.almacen_desde_entorno()
    assert isinstance(a, alm.AlmacenLocal) and not a.existe("cnv/nada")
    a.guardar("cnv/x", b"1")
    assert a.existe("cnv/x")
    instalar_gcs(monkeypatch)
    monkeypatch.setenv("GCS_BUCKET", "b")
    monkeypatch.setenv("GCP_SA_KEY", json.dumps({"project_id": "p"}))
    assert isinstance(alm.almacen_desde_entorno(), alm.AlmacenGCS)


# --- modelo: respuestas mal formadas ---------------------------------------------------------

def cliente_con(texto=None, stop="end_turn", bloques=None):
    contenido = bloques if bloques is not None else [SimpleNamespace(type="text", text=texto)]
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(stop_reason=stop, content=contenido)))


@pytest.mark.parametrize("cliente,mensaje", [
    (cliente_con("{no es json"), "incompleta|sin texto|invalido|Expecting|Extra"),
    (cliente_con("", ), "Expecting"),
    (cliente_con("{}", stop="max_tokens"), "incompleta"),
    (cliente_con("{}", stop="refusal"), "incompleta"),
    (cliente_con(bloques=[]), "sin texto"),
])
def test_financieros_respuesta_mal_formada_lanza(cliente, mensaje):
    with pytest.raises(Exception, match=mensaje):
        fin.extraer_con_claude("recorte", cliente)


@pytest.mark.parametrize("r", [
    {"tipo": "semestral", "periodo": "2026-06-30", "unidad": "miles de pesos", "cifras": "no es lista"},
    {"tipo": "semestral", "periodo": "2026-06-30", "unidad": None, "cifras": [None, 5, "x", {"campo": "ventas"}, {"campo": "ventas", "valor": True, "cita": "a b c"}]},
    {"tipo": "semestral", "periodo": "2026-06-30", "cifras": [{"campo": "ventas", "valor": "18.400.250", "cita": CITAS["ventas"]}]},
    {"tipo": "inventado", "periodo": "2026-06-30", "cifras": []},
    {"tipo": "anual", "periodo": "30/06/2026", "cifras": []},
    {"tipo": "anual", "periodo": "2999-01-01", "cifras": []},
    {"tipo": "anual", "periodo": "2026-13-45", "cifras": []},
    {"tipo": "anual", "periodo": None, "cifras": []},
])
def test_financieros_contenido_malo_no_publica_nada(r):
    out = fin.procesar_texto(BALANCE, ClienteFalso(r))
    assert out["estado"] in ("sin_cifras", "no_es_estado_contable", "sin_periodo")


def test_financieros_texto_enorme_arma_un_recorte_acotado():
    paginas = "\n\n".join(f"[pagina {i}]\n" + ("Estado de situacion financiera total del activo " * 400) for i in range(1, 301))
    recorte, elegidas = fin.armar_recorte(paginas)
    assert len(recorte) <= fin.MAX_CARACTERES + 200 and 1 in elegidas and len(elegidas) < 300
    assert fin.armar_recorte("") == ("", []) and fin.armar_recorte("[pagina 1]\nsolo portada sin palabras clave") == ("", [])
    assert fin.dividir_paginas("sin marcas") == [(1, "sin marcas")] and fin.dividir_paginas("  ") == []
    assert fin.texto_util("") is False


def test_financieros_ejecutar_con_documento_vacio_ajeno_y_corte_de_tiempo(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, EMP["cuit"], [
        {"id": "1", "t": "estados_contables", "f": "2026-08-01", "texto_ok": True, "d": "EECC"},                                                    # sin texto en el almacen
        {"id": "2", "t": "estados_contables", "f": "2026-08-02", "texto_ok": True, "d": "CUIT: 30-99999999-1 TIPO BALANCE: CONSOLIDADO"},        # de otra entidad
        {"id": "3", "t": "estados_contables", "f": "2026-08-03", "texto_ok": True, "d": "EECC"},
        {"id": "4", "t": "estados_contables", "f": "2026-08-04", "texto_ok": True, "d": "EECC"},
    ])
    alm.guardar_texto(a, EMP["cuit"], "2", BALANCE)
    alm.guardar_texto(a, EMP["cuit"], "3", "[pagina 1]\nescaneado")
    alm.guardar_texto(a, EMP["cuit"], "4", BALANCE)
    c = ClienteFalso(respuesta())
    t = fin.ejecutar(a, [EMP], c, max_docs=10, hasta_t=1.0)  # tiempo vencido antes de empezar
    assert t["pendientes"] == 4 and c.pedidos == []
    t = fin.ejecutar(a, [EMP], c, max_docs=10)
    estados = {k: v["estado"] for k, v in fin.leer_financieros(a, EMP["cuit"])["procesados"].items()}
    assert estados == {"4": "ok", "3": "escaneado", "2": "otra_entidad"} and len(c.pedidos) == 1 and "1" not in estados
    assert t["ok"] == 1 and t["saltados"] == 2
    t = fin.ejecutar(a, [EMP], ClienteFalso(respuesta()), max_docs=0)  # tope de documentos: ya no hay pendientes con texto
    assert t["procesados"] == 0


def test_financieros_error_de_api_se_reintenta_hasta_el_maximo(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, EMP["cuit"], [{"id": "4", "t": "estados_contables", "f": "2026-08-04", "texto_ok": True, "d": "EECC"}])
    alm.guardar_texto(a, EMP["cuit"], "4", BALANCE)
    c = ClienteFalso(respuesta(), stop_reason="refusal")
    for _ in range(fin.MAX_INTENTOS + 2):
        fin.ejecutar(a, [EMP], c, max_docs=10)
    p = fin.leer_financieros(a, EMP["cuit"])["procesados"]["4"]
    assert len(c.pedidos) == fin.MAX_INTENTOS and p["estado"] == "error" and p["intentos"] == fin.MAX_INTENTOS and "refusal" in p["error"]


def test_financieros_funciones_sueltas():
    assert fin.tipo_balance({"d": "TIPO BALANCE: Consolidado"}) == "consolidado" and fin.tipo_balance({"d": "TIPO BALANCE: Individual"}) == "individual"
    assert fin.tipo_balance({"d": "TIPO BALANCE: Separado"}) == "individual" and fin.tipo_balance({}) == "desconocido"
    assert fin.entidad_ajena({"d": "sin cuit"}, EMP) is False and fin.entidad_ajena({"d": "CUIT 30-11111111-2"}, EMP) is False
    assert fin._coherencia_balance({"activo_total": {"valor": 0}, "pasivo_total": {"valor": 0}, "patrimonio_neto": {"valor": 0}}) is None
    assert fin._coherencia_balance({}) is None
    assert fin.periodo_valido(5) is False and fin.periodo_valido("2026-02-31") is False and fin.periodo_valido("1980-01-01") is False
    assert fin.a_numero("0.5") == 0.5 and fin.a_numero("1,2,3x") is None
    assert fin.numero_respaldado(-350500, "Perdida del periodo 350.500") is True and fin.numero_respaldado(5, "no hay numeros") is False
    assert fin.numero_respaldado(-5, "Resultado 5") is False


# --- resumenes ---------------------------------------------------------------------------------

def test_resumir_respuestas_raras():
    assert resumenes.resumir(cliente_con("x", stop="refusal"), "E", "T", "texto") == ""
    assert resumenes.resumir(cliente_con(bloques=[]), "E", "T", "texto") == ""
    assert resumenes.resumir(cliente_con("   "), "E", "T", "texto") == ""
    assert resumenes.limpiar_resumen(None) == ""
    enorme = "[pagina 1]\n" + "palabra " * 200_000
    visto = {}
    cliente = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: visto.update(kw) or SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="ok")])))
    assert resumenes.resumir(cliente, "E", "T", enorme) == "ok"
    assert len(visto["messages"][0]["content"]) < resumenes.MAX_CARACTERES_ENTRADA + 500  # texto enorme: se manda solo el comienzo


def test_resumenes_ejecutar_errores_vacios_y_topes(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, EMP["cuit"], [{"id": str(i), "t": "hechos", "f": f"2026-10-0{i}", "texto_ok": True, "d": "t"} for i in range(1, 6)])
    for i in (1, 2, 3, 4):
        alm.guardar_texto(a, EMP["cuit"], str(i), f"[pagina 1]\nhecho {i}")
    respuestas = iter([ConnectionError("cae"), "", "Resumen ok", "Otro ok"])

    def crear(**kw):
        x = next(respuestas)
        if isinstance(x, Exception):
            raise x
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=x)])

    t = resumenes.ejecutar(a, [EMP], SimpleNamespace(messages=SimpleNamespace(create=crear)), max_docs=10)
    assert t == {"resumidos": 2, "errores": 2, "pendientes": 0}
    t = resumenes.ejecutar(a, [EMP], SimpleNamespace(messages=SimpleNamespace(create=crear)), max_docs=1)
    assert t["pendientes"] >= 1
    t = resumenes.ejecutar(a, [EMP], SimpleNamespace(messages=SimpleNamespace(create=crear)), max_docs=10, hasta_t=1.0)
    assert t["pendientes"] >= 1


def test_resumenes_guarda_de_a_tandas(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, EMP["cuit"], [{"id": str(i), "t": "hechos", "f": "2026-10-01", "texto_ok": True, "d": "t"} for i in range(1, 26)])
    for i in range(1, 26):
        alm.guardar_texto(a, EMP["cuit"], str(i), f"[pagina 1]\nhecho {i}")
    c = cliente_con("Resumen")
    assert resumenes.ejecutar(a, [EMP], c, max_docs=100)["resumidos"] == 25
    assert all(m.get("r") == "Resumen" for m in alm.leer_indice_empresa(a, EMP["cuit"]))


# --- avisos y clasificacion: textos enormes -----------------------------------------------------

def test_aviso_enorme_se_recorta_para_el_modelo_pero_se_verifica_contra_todo(tmp_path):
    from .test_avisos_pago import AVISO, BUENOS, Cliente, resp

    cola = "\n[pagina 2]\n" + ("relleno " * 20_000) + "\nTasa de interes aplicable al periodo: 7,5% nominal anual.\n"
    texto = AVISO.replace("Tasa de interes aplicable al periodo: 7,5% nominal anual.", "") + cola
    c = Cliente(resp(BUENOS))
    r = avp.procesar_texto(texto, c)
    enviado = c.pedidos[0]["messages"][0]["content"]
    assert len(enviado) < avp.MAX_CARACTERES + 100 and r["estado"] == "ok" and r["pagos"][0]["tasa"]["pagina"] == 2


def test_clasificacion_recorta_la_entrada():
    visto = {}

    def crear(**kw):
        visto.update(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"categoria": "otro", "cita": ""}))])

    cat, motivo = cla.clasificar(SimpleNamespace(messages=SimpleNamespace(create=crear)), "E", "T", "[pagina 1]\n" + "x " * 100_000)
    assert (cat, motivo) == ("otro", None) and len(visto["messages"][0]["content"]) < cla.MAX_CARACTERES_ENTRADA + 600


def test_avisos_funciones_sueltas():
    assert avp._fecha_plausible("2026-02-30") is False and avp._fecha_plausible("26-01-01") is False
    assert avp._moneda_de_cita("pagos en US$ y $ mezclados") is None and avp._moneda_de_cita("monto en ARS 100") == "pesos" and avp._moneda_de_cita("sin simbolo") is None
    assert avp.ya_procesado({"procesados": {}}, "1") is False
    assert avp.ya_procesado({"procesados": {"1": {"estado": "ok", "v": "0"}}}, "1") is True


def test_modelo_pedir_json_rechaza_lo_que_no_es_objeto():
    from cnv.modelo import pedir_json

    with pytest.raises(RuntimeError, match="objeto"):
        pedir_json(cliente_con("[1,2]"), "s", "e", {})
    with pytest.raises(RuntimeError, match="json invalido"):
        pedir_json(cliente_con("{x"), "s", "e", {})


# --- linea de comandos --------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["resumenes", "financieros", "clasificar", "avisos-pago"])
def test_cli_sin_clave_de_api_no_hace_nada(cmd, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP])
    assert cli.main([cmd]) == 2 and "falta ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_cli_sync_empresa_con_error_y_corte_por_tiempo(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    otra = {"empresa": "OTRA", "cuit": "30222222223"}
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP, otra])

    def sync(almacen, e, *a, **k):
        if e is EMP:
            raise ConnectionError("cae")
        return {"nuevos": 1, "fallidos": 0, "pendientes": 0}

    monkeypatch.setattr(cli, "sincronizar_empresa", sync)
    assert cli.main(["sync", "--solo", "30-22222222-3"]) == 0
    assert "+1 nuevos" in capsys.readouterr().out
    assert cli.main(["sync"]) == 0
    salida = capsys.readouterr().out
    assert "ERROR cae" in salida and "'empresas_con_error': 1" in salida
    assert cli.main(["sync", "--tiempo-max", "-1"]) == 0
    assert "corte por tiempo" in capsys.readouterr().out
    assert cli.main(["sync", "--fragmento", "1/2"]) == 0
    assert "OTRA" in capsys.readouterr().out


def test_cli_modelos_con_cliente_falso(monkeypatch, tmp_path, capsys):
    import anthropic

    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "de-prueba")
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP])
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: AnthropicFalso({}))
    for cmd in (["resumenes"], ["financieros", "--solo", "30-11111111-2"], ["clasificar"], ["avisos-pago", "--solo", "30111111112"]):
        assert cli.main(cmd) == 0
    assert capsys.readouterr().out.count("TOTAL") == 4


def test_cli_ocr_recorre_las_empresas(monkeypatch, tmp_path, capsys):
    from cnv import ocr

    monkeypatch.setenv("DATOS_LOCAL", str(tmp_path))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [EMP, {"empresa": "OTRA", "cuit": "30222222223"}])
    monkeypatch.setattr(ocr, "comprobar", lambda idioma="spa": None)
    monkeypatch.setattr(cli, "reprocesar_escaneados", lambda *a, **k: {"ocr": 2, "sin_texto": 1, "pendientes": 0})
    assert cli.main(["ocr", "--solo", "30111111112"]) == 0
    salida = capsys.readouterr().out
    assert "2 con texto por OCR" in salida and "OTRA" not in salida
    monkeypatch.delenv("CNV_OCR", raising=False)
