"""Fase 1: mas tipos de documento, topes de tamano, reintentos y resultados parciales (todo sin red)."""
import datetime as dt
import gzip
import io
import json
import os
import zipfile
from types import SimpleNamespace

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import descarga, listado, sincronizar
from cnv.descarga import ErrorDescarga, Limites

from .pdfs import pdf_cifrado, pdf_con_paginas

AQUI = os.path.dirname(__file__)
LISTADO = open(os.path.join(AQUI, "fixtures", "listado_completo.html"), encoding="utf-8").read()


# --- listado ---------------------------------------------------------------

def test_listado_clasifica_los_tipos_nuevos():
    por_id = {d["doc_id"]: d["tipo"] for d in listado.parsear_listado(LISTADO)}
    assert por_id["3400100"] == por_id["3400200"] == por_id["3400300"] == por_id["3400400"] == "prospectos"
    assert por_id["3490000"] == "avisos_pago"
    assert por_id["3560000"] == por_id["3560100"] == "actas"
    assert por_id["3570000"] == "otros"  # lo que no se conoce queda como metadato
    assert set(sincronizar.TIPOS_TEXTO) >= {"prospectos", "avisos_pago", "actas"}


# --- topes -----------------------------------------------------------------

def test_limites_desde_entorno_con_valores_invalidos():
    assert Limites.desde_entorno({}) == Limites()
    assert Limites.desde_entorno({"CNV_MAX_PAGINAS": "50", "CNV_MAX_CARACTERES": "1000", "CNV_MAX_MB": "5"}) == Limites(50, 1000, 5)
    # basura, cero y negativos caen al defecto: un typo en el workflow no debe dejar la base sin texto
    assert Limites.desde_entorno({"CNV_MAX_PAGINAS": "abc", "CNV_MAX_CARACTERES": "0", "CNV_MAX_MB": "-3"}) == Limites()


def test_extraer_pdf_trunca_por_paginas():
    pdf = pdf_con_paginas([f"pagina numero {i}" for i in range(1, 11)])
    r = descarga.extraer_pdf(pdf, max_paginas=4)
    assert (r["paginas"], r["leidas"], r["truncado"]) == (10, 4, "paginas")
    assert "[pagina 4]" in r["texto"] and "[pagina 5]" not in r["texto"]


def test_extraer_pdf_trunca_por_caracteres_en_limite_de_pagina():
    pdf = pdf_con_paginas(["x" * 300 for _ in range(10)])
    r = descarga.extraer_pdf(pdf, max_paginas=400, max_caracteres=1000)
    assert r["truncado"] == "caracteres" and 1 <= r["leidas"] < 10
    assert len(r["texto"]) <= 1000
    assert r["texto"].endswith("x")  # corta entre paginas, no a mitad de una


def test_extraer_pdf_pagina_gigante_se_recorta():
    r = descarga.extraer_pdf(pdf_con_paginas(["y" * 5000]), max_caracteres=500)
    assert r["truncado"] == "caracteres" and len(r["texto"]) == 500 and r["leidas"] == 1


def test_extraer_pdf_sin_truncar_no_marca_nada():
    r = descarga.extraer_pdf(pdf_con_paginas(["a", "b"]), max_paginas=2)
    assert r["truncado"] is None and r["leidas"] == 2


def test_extraer_pdf_cifrado_y_roto():
    assert descarga.extraer_pdf(pdf_cifrado(["secreto"]))["cifrado"] is True
    assert descarga.extraer_pdf(b"%PDF-1.4 esto no es un pdf")["ilegible"] is True


# --- descarga con reintentos -----------------------------------------------

class Resp:
    def __init__(self, status=200, contenido=b"", datos=None):
        self.status_code, self.content, self._datos = status, contenido, datos

    def json(self):
        if self._datos is None:
            raise ValueError("no es json")
        return self._datos


class SesionFalsa:
    """Doble de requests.Session. `blobs[guid]` y `llaves[guid]` son listas de respuestas/excepciones que se consumen
    una por intento (si `llaves` no tiene el guid, la llave siempre sale bien)."""

    def __init__(self, pagina="", blobs=None, llaves=None):
        self.pagina, self.blobs, self.llaves, self.llamadas = pagina, blobs or {}, llaves or {}, []

    @staticmethod
    def _dar(x):
        if isinstance(x, Exception):
            raise x
        return x

    def get(self, url, timeout=None):
        if "GetPublicValetKey" in url:
            guid = url.split("GetPublicValetKey/")[1].split("?")[0]
            self.llamadas.append(guid)
            cola = self.llaves.get(guid)
            return self._dar(cola.pop(0)) if cola else Resp(200, datos={"valetKeyData": "k"})
        return SimpleNamespace(text=self.pagina, encoding=None, raise_for_status=lambda: None)

    def post(self, url, data=None, timeout=None):
        return self._dar(self.blobs[url.rsplit("/", 1)[1]].pop(0))


def pagina(*nombres_guids):
    lista = json.dumps([{"guid": g, "nombreArchivo": n} for n, g in nombres_guids])
    return f'<propiedad uploader="x">{lista.replace(chr(34), "&quot;")}</propiedad>'


def test_descargar_reintenta_con_espera_creciente():
    esperas = []
    # 1er intento: error de red al pedir la llave; 2do: 503 en la llave; 3ro: bien
    s = SesionFalsa(llaves={"g1": [ConnectionError("cortado"), Resp(503)]}, blobs={"g1": [Resp(200, b"%PDF" + b"x" * 200)]})
    r = descarga.descargar_bytes("g1", s, dormir=esperas.append, espera=2)
    assert r.startswith(b"%PDF")
    assert esperas == [2, 4]


def test_descargar_agota_intentos_y_lanza():
    s = SesionFalsa(llaves={"g": [ConnectionError("a"), ConnectionError("b"), ConnectionError("c")]})
    with pytest.raises(ErrorDescarga) as e:
        descarga.descargar_bytes("g", s, intentos=3, dormir=lambda t: None)
    assert "ConnectionError" in e.value.motivo and len(s.llamadas) == 3


def test_descargar_no_reintenta_errores_definitivos_ni_archivos_enormes():
    s = SesionFalsa(blobs={"g": [Resp(404, b"no")]})
    with pytest.raises(ErrorDescarga):
        descarga.descargar_bytes("g", s, dormir=lambda t: None)
    assert len(s.llamadas) == 1
    s = SesionFalsa(blobs={"g": [Resp(200, b"0" * 5000)]})
    with pytest.raises(ErrorDescarga) as e:
        descarga.descargar_bytes("g", s, max_bytes=1000, dormir=lambda t: None)
    assert not e.value.reintentable


def test_descargar_adjunto_vacio_devuelve_none():
    assert descarga.descargar_bytes("g", SesionFalsa(blobs={"g": [Resp(200, b"corto")]})) is None


def test_descargar_respeta_el_tope_de_tiempo(monkeypatch):
    s = SesionFalsa(llaves={"g": [ConnectionError("a")] * 4})
    pausas = []
    ahora = descarga.time.monotonic()
    with pytest.raises(ErrorDescarga):
        descarga.descargar_bytes("g", s, espera=100, hasta_t=ahora + 5, dormir=pausas.append)
    assert pausas == [] and len(s.llamadas) == 1  # no duerme 100 s si el documento solo tiene 5 s


# --- texto de una presentacion: parcial, truncado, cifrado ------------------

def test_presentacion_parcial_conserva_el_adjunto_bueno():
    bueno = pdf_con_paginas(["Texto del prospecto con ofertas y condiciones"])
    s = SesionFalsa(pagina(("a.pdf", "ga"), ("b.pdf", "gb")), blobs={"ga": [Resp(200, bueno)], "gb": [Resp(404, b"x")]})
    r = descarga.texto_de_presentacion("https://x/publicview/p", s, dormir=lambda t: None)
    assert "Texto del prospecto" in r["texto"] and r["archivos"] == ["a.pdf"]
    assert r["fallos"] == [{"archivo": "b.pdf", "motivo": "http 404"}]


def test_presentacion_solo_fallos_devuelve_los_motivos():
    s = SesionFalsa(pagina(("a.pdf", "ga")), blobs={"ga": [Resp(404, b"x")]})
    r = descarga.texto_de_presentacion("u", s, dormir=lambda t: None)
    assert r == {"fallos": [{"archivo": "a.pdf", "motivo": "http 404"}]}


def test_presentacion_registra_truncado_y_comparte_el_cupo_entre_adjuntos():
    grande = pdf_con_paginas([f"pagina {i}" for i in range(1, 8)])
    s = SesionFalsa(pagina(("a.pdf", "ga"), ("b.pdf", "gb")), blobs={"ga": [Resp(200, grande)], "gb": [Resp(200, grande)]})
    r = descarga.texto_de_presentacion("u", s, limites=Limites(paginas=10), dormir=lambda t: None)
    # 7 paginas del primero + 3 del segundo (cupo de 10 paginas por documento)
    assert r["truncado"] == {"motivo": "paginas", "paginas_total": 7, "paginas_leidas": 3}
    assert r["texto"].count("[pagina ") == 10 and r["paginas"] == 14


def test_presentacion_pdf_cifrado_y_zip_sin_pdf_son_fallos():
    s = SesionFalsa(pagina(("a.pdf", "ga"), ("b.zip", "gb"), ("c.bin", "gc")),
                    blobs={"ga": [Resp(200, pdf_cifrado(["x"]))], "gb": [Resp(200, b"PK\x03\x04" + b"0" * 200)], "gc": [Resp(200, b"1" * 300)]})
    r = descarga.texto_de_presentacion("u", s, dormir=lambda t: None)
    assert {f["archivo"]: f["motivo"] for f in r["fallos"]} == {"a.pdf": "pdf cifrado", "b.zip": "no es PDF ni ZIP con PDF", "c.bin": "no es PDF ni ZIP con PDF"}
    assert "texto" not in r


def test_presentacion_sin_adjuntos_devuelve_vacio():
    assert descarga.texto_de_presentacion("u", SesionFalsa("<html></html>")) == {}


def test_presentacion_zip_con_varios_pdf():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("uno.pdf", pdf_con_paginas(["primero"]))
        z.writestr("dos.pdf", pdf_con_paginas(["segundo"]))
    s = SesionFalsa(pagina(("todo.zip", "gz")), blobs={"gz": [Resp(200, buf.getvalue())]})
    r = descarga.texto_de_presentacion("u", s)
    assert r["archivos"] == ["uno.pdf", "dos.pdf"] and "primero" in r["texto"] and "segundo" in r["texto"]


# --- sincronizacion --------------------------------------------------------

EMP = {"empresa": "EMPRESA EJEMPLO S.A.", "cuit": "30111111112"}


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    monkeypatch.setattr(sincronizar, "listar", lambda cuit, desde: listado.parsear_listado(LISTADO))
    monkeypatch.setattr(sincronizar, "sesion", lambda: None)
    monkeypatch.setattr(sincronizar.time, "sleep", lambda s: None)
    return alm.AlmacenLocal(str(tmp_path))


def test_sync_baja_todos_los_tipos_y_deja_otros_solo_como_metadato(entorno, monkeypatch):
    pedidos = []

    def falso(url, s=None, limites=None, hasta_t=None):
        pedidos.append(url)
        return {"texto": f"[pagina 1]\n{url}", "paginas": 1, "archivos": ["a.pdf"], "escaneado": False, "truncado": None, "fallos": []}

    monkeypatch.setattr(sincronizar, "texto_de_presentacion", falso)
    r = sincronizar.sincronizar_empresa(entorno, EMP, dt.date(2026, 1, 1))
    assert r == {"nuevos": 9, "fallidos": 0, "pendientes": 0}
    assert not any(u.endswith("/o1") for u in pedidos)
    indice = {m["id"]: m for m in alm.leer_indice_empresa(entorno, EMP["cuit"])}
    assert indice["3570000"]["texto_ok"] is False
    assert {indice[i]["t"] for i in indice if indice[i]["texto_ok"]} == {"hechos", "estados_contables", "prospectos", "avisos_pago", "actas"}


def test_sync_registra_truncado_y_lo_expone_en_el_indice_global(entorno, monkeypatch):
    trunc = {"motivo": "paginas", "paginas_total": 812, "paginas_leidas": 400}
    monkeypatch.setattr(sincronizar, "texto_de_presentacion", lambda url, s=None, limites=None, hasta_t=None: {
        "texto": "[pagina 1]\nProspecto", "paginas": 812, "archivos": ["p.pdf"], "escaneado": False, "truncado": trunc if url.endswith("/p1") else None, "fallos": []})
    sincronizar.sincronizar_empresa(entorno, EMP, dt.date(2026, 1, 1), ("prospectos",))
    indice = {m["id"]: m for m in alm.leer_indice_empresa(entorno, EMP["cuit"])}
    assert indice["3400100"]["trunc"] == trunc and "trunc" not in indice["3400200"]
    sincronizar.construir_indice_global(entorno, [EMP])
    filas = {f["id"]: f for f in json.loads(gzip.decompress(entorno.leer("cnv/indice/global.json.gz")))}
    assert filas["3400100"]["tr"] == "paginas" and "tr" not in filas["3400200"]


def test_sync_pasa_los_limites_a_la_descarga(entorno, monkeypatch):
    vistos = []
    monkeypatch.setattr(sincronizar, "texto_de_presentacion", lambda url, s=None, limites=None, hasta_t=None: vistos.append(limites) or {})
    sincronizar.sincronizar_empresa(entorno, EMP, dt.date(2026, 1, 1), ("avisos_pago",), limites=Limites(7, 70, 1))
    assert vistos == [Limites(7, 70, 1)]


def test_parcial_se_reintenta_y_conserva_el_texto_si_no_mejora(entorno, monkeypatch):
    estados = iter([
        {"texto": "[pagina 1]\ntexto largo del primer adjunto", "paginas": 1, "archivos": ["a.pdf"], "truncado": None, "fallos": [{"archivo": "b.pdf", "motivo": "http 503"}]},
        {"texto": "[pagina 1]\ncorto", "paginas": 1, "archivos": ["a.pdf"], "truncado": None, "fallos": [{"archivo": "b.pdf", "motivo": "http 503"}]},
        {"texto": "[pagina 1]\ntexto largo del primer adjunto\n\n[pagina 1]\nsegundo adjunto", "paginas": 2, "archivos": ["a.pdf", "b.pdf"], "truncado": None, "fallos": []},
    ])
    monkeypatch.setattr(sincronizar, "texto_de_presentacion", lambda url, s=None, limites=None, hasta_t=None: next(estados))
    args = (entorno, EMP, dt.date(2026, 1, 1), ("avisos_pago",))
    sincronizar.sincronizar_empresa(*args)
    m = {x["id"]: x for x in alm.leer_indice_empresa(entorno, EMP["cuit"])}["3490000"]
    assert m["texto_ok"] and m["parcial"] == 1 and m["intentos"] == 1 and "b.pdf" in m["error"]
    sincronizar.sincronizar_empresa(*args)  # el reintento trae menos texto: no pisa el que estaba
    assert alm.leer_texto(entorno, EMP["cuit"], "3490000").endswith("primer adjunto")
    m = {x["id"]: x for x in alm.leer_indice_empresa(entorno, EMP["cuit"])}["3490000"]
    assert m["intentos"] == 2 and m["parcial"] == 1
    sincronizar.sincronizar_empresa(*args)  # ahora si completo: se limpian los rastros del parcial
    m = {x["id"]: x for x in alm.leer_indice_empresa(entorno, EMP["cuit"])}["3490000"]
    assert m["texto_ok"] and "parcial" not in m and "error" not in m
    assert "segundo adjunto" in alm.leer_texto(entorno, EMP["cuit"], "3490000")
    assert sincronizar.pendientes(listado.parsear_listado(LISTADO), list(alm.leer_indice_empresa(entorno, EMP["cuit"])), ("avisos_pago",)) == []


def test_parcial_deja_de_reintentarse_al_agotar_intentos():
    doc = {"doc_id": "1", "tipo": "actas"}
    base = {"id": "1", "texto_ok": True, "parcial": 1}
    assert sincronizar.pendientes([doc], [{**base, "intentos": 2}], ("actas",)) == [doc]
    assert sincronizar.pendientes([doc], [{**base, "intentos": 3}], ("actas",)) == []


def test_fallo_total_guarda_el_motivo_de_cada_adjunto(entorno, monkeypatch):
    monkeypatch.setattr(sincronizar, "texto_de_presentacion", lambda url, s=None, limites=None, hasta_t=None: {"fallos": [{"archivo": "a.pdf", "motivo": "pdf cifrado"}]})
    r = sincronizar.sincronizar_empresa(entorno, EMP, dt.date(2026, 1, 1), ("actas",))
    assert r["fallidos"] == 2
    m = {x["id"]: x for x in alm.leer_indice_empresa(entorno, EMP["cuit"])}["3560000"]
    assert m["error"] == "a.pdf: pdf cifrado" and m["intentos"] == 1


def test_un_corte_a_mitad_no_pierde_lo_ya_bajado(entorno, monkeypatch):
    n = {"v": 0}

    def falso(url, s=None, limites=None, hasta_t=None):
        n["v"] += 1
        if n["v"] > 3:
            raise KeyboardInterrupt  # el job se cancela a la mitad
        return {"texto": "[pagina 1]\nok", "paginas": 1, "archivos": ["a.pdf"], "truncado": None, "fallos": []}

    monkeypatch.setattr(sincronizar, "texto_de_presentacion", falso)
    monkeypatch.setattr(sincronizar, "GUARDAR_CADA", 2)
    with pytest.raises(BaseException):
        sincronizar.sincronizar_empresa(entorno, EMP, dt.date(2026, 1, 1), ("hechos", "estados_contables", "prospectos", "actas"), trabajadores=1)
    guardados = [m for m in alm.leer_indice_empresa(entorno, EMP["cuit"]) if m.get("texto_ok")]
    assert len(guardados) == 3  # el indice se escribio antes de salir: la proxima corrida no rebaja estos 3


# --- linea de comandos -----------------------------------------------------

def test_cli_rechaza_tipos_desconocidos(capsys):
    assert cli.main(["sync", "--tipos", "hechos,inventado", "--solo", "30111111112"]) == 2
    assert "inventado" in capsys.readouterr().out
