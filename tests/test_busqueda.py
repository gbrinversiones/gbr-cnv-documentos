"""Fase 9: indice de busqueda de texto completo. El formato es un contrato con el lector en JS: estas pruebas lo fijan."""
import datetime as dt
import gzip
import json
import math
import os
import re
import zlib

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import busqueda as bus

from . import generar_oraculo

AQUI = os.path.dirname(__file__)
ORACULO = json.load(open(os.path.join(AQUI, "fixtures", "busqueda_oraculo.json"), encoding="utf-8"))
AHORA = dt.datetime(2026, 10, 16, 12, 0, 0, tzinfo=dt.timezone.utc)


# --- tokenizacion -------------------------------------------------------------------

@pytest.mark.parametrize("texto,esperado", [
    ("Obligaciones NEGOCIABLES Clase 5", ["obligaciones", "negociables", "clase"]),         # minusculas; "5" tiene menos de 3 letras
    ("Calificación de riesgo — energía", ["calificacion", "riesgo", "energia"]),             # acentos y rayas
    ("ÁÉÍÓÚ ñandú", ["aeiou", "nandu"]),                                                      # la ñ se descompone en n + marca
    ("Resolución 2026-10-15 N° 1234", ["resolucion", "2026", "1234"]),                          # numeros y separadores
    ("de la el por S.A. CNV sociedad anonima empresa documento", []),                          # stopwords
    ("ab " + "x" * 31 + " " + "y" * 30 + " " + "z" * 3, ["y" * 30, "zzz"]),                    # largo: < 3 y > 30 se descartan
    ("a1b2c3 foo_bar foo-bar", ["a1b2c3", "foo", "bar", "foo", "bar"]),
    ("", []), (None, []), ("   \n\t", []),
    ("日本語 texto", ["texto"]),                                                                 # lo que no es [a-z0-9] corta el token
])
def test_tokenizar(texto, esperado):
    assert bus.tokenizar(texto) == esperado


def test_tokenizar_coincide_con_el_fixture_compartido():
    for entrada, tokens in ORACULO["tokenizacion"]:
        assert bus.tokenizar(entrada) == tokens


def test_stopwords_son_una_lista_fija_en_minuscula_sin_acentos():
    assert {"sociedad", "anonima", "cnv", "empresa", "documento", "para", "con", "del", "los"} <= bus.STOPWORDS
    assert all(re.fullmatch(r"[a-z0-9]+", w) for w in bus.STOPWORDS)
    assert sorted(bus.STOPWORDS) == ORACULO["stopwords"]


def test_la_consulta_se_tokeniza_igual_que_los_documentos():
    assert bus.tokenizar("¿Recompra de Obligaciones Negociables?") == bus.tokenizar("recompra obligaciones negociables")


# --- shards ----------------------------------------------------------------------------

def test_shard_estable_con_valores_literales_de_crc32():
    assert zlib.crc32(b"hello") == 907060870 and bus.shard_de("hello") == 907060870 % 64 == 6
    assert zlib.crc32(b"hello world") == 222957957 and bus.shard_de("hello world") == 5
    assert zlib.crc32("obligaciones".encode("utf-8")) == 261616078 and bus.shard_de("obligaciones") == 14
    assert bus.shard_de("dividendos") == 3266210801 % 64 == 49 and bus.shard_de("bonos") == 33 and bus.shard_de("recompra") == 26
    assert bus.shard_de("energía") == zlib.crc32("energía".encode("utf-8")) % 64 == 1      # utf-8, sin normalizar
    assert {t: bus.shard_de(t) for t in ORACULO["shards_literales"]} == ORACULO["shards_literales"]


def test_nombres_de_shard():
    assert bus.nombre_shard(0) == "cnv/busqueda/s00.json.gz" and bus.nombre_shard(63) == "cnv/busqueda/s63.json.gz"


# --- construccion: 20 documentos sinteticos ----------------------------------------------------

def corpus_20(a):
    """20 documentos de 2 empresas: temas repartidos para que haya terminos raros (df 1), medios y comunes (> 40%)."""
    meta = {"30111111112": [], "30222222223": []}
    for i in range(1, 21):
        cuit = "30111111112" if i <= 10 else "30222222223"
        tipo = ("hechos", "prospectos", "avisos_pago", "actas")[i % 4]
        partes = ["comunicado habitual del directorio"]           # 'comunicado', 'habitual', 'directorio' en el 100%: demasiado comunes
        if i % 2 == 0:
            partes.append("dividendos efectivo")                    # df = 10 (50%): demasiado comun
        if i % 5 == 0:
            partes.append("recompra obligaciones")                  # df = 4 (20%): entra
        if i == 7:
            partes.append("raritotermino")                          # df = 1: no entra
        if i in (3, 4):
            partes.append("repetido " * 400)                        # tf 800 -> acotado a 255
        alm.guardar_texto(a, cuit, str(100 + i), "[pagina 1]\n" + " ".join(partes))
        meta[cuit].append({"id": str(100 + i), "t": tipo, "texto_ok": True})
    for cuit, docs in meta.items():
        alm.guardar_indice_empresa(a, cuit, docs)
    return [{"empresa": c, "cuit": c} for c in meta]


@pytest.fixture
def base(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    return a, corpus_20(a)


def gz(a, nombre):
    return json.loads(gzip.decompress(a.leer(nombre)))


def test_formato_de_los_archivos(base):
    a, universo = base
    r = bus.construir(a, universo, ahora=AHORA)
    assert r["estado"] == "ok" and r["n_docs"] == 20
    # meta.json
    meta = json.loads(a.leer("cnv/busqueda/meta.json"))
    assert meta == {"version": 1, "n_docs": 20, "avgdl": meta["avgdl"], "shards": 64, "generado": "2026-10-16T12:00:00Z"}
    assert list(meta) == ["version", "n_docs", "avgdl", "shards", "generado"]
    # docs.json.gz: [cuit, doc_id, longitud_en_tokens] en orden (cuit, id numerico)
    docs = gz(a, "cnv/busqueda/docs.json.gz")
    assert [d[:2] for d in docs] == [["30111111112", str(100 + i)] for i in range(1, 11)] + [["30222222223", str(100 + i)] for i in range(11, 21)]
    assert all(isinstance(d[2], int) and d[2] > 0 for d in docs)
    assert meta["avgdl"] == pytest.approx(sum(d[2] for d in docs) / 20, abs=1e-4)
    # longitud = tokens que quedan: "comunicado habitual directorio" (3; "del" es stopword) + "dividendos efectivo" en los pares + "recompra obligaciones" en los multiplos de 5
    assert (docs[0][2], docs[1][2], docs[4][2], docs[9][2]) == (3, 5, 5, 7)
    # los 64 shards existen; cada termino esta en el shard que dice crc32 % 64 y en ningun otro
    visto = {}
    for k in range(64):
        for termino, postings in gz(a, bus.nombre_shard(k)).items():
            assert bus.shard_de(termino) == k and termino not in visto
            visto[termino] = postings
    assert set(visto) == {"recompra", "obligaciones", "repetido"}        # df>=2 y df<=40%
    assert [p[0] for p in visto["recompra"]] == [4, 9, 14, 19]            # doc_idx de i=5,10,15,20 (0-indexado)
    assert all(p[1] == 1 for p in visto["recompra"])
    assert visto["repetido"] == [[2, 255], [3, 255]]                      # tf acotado a 255
    for postings in visto.values():
        assert postings == sorted(postings) and all(isinstance(x, int) for p in postings for x in p)
    assert not {"comunicado", "dividendos", "raritotermino", "directorio"} & set(visto)   # demasiado comunes o demasiado raros


def test_los_umbrales_de_df_son_exactos(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    # 10 documentos: df=4 es exactamente 40% (entra), df=5 es 50% (no), df=2 entra, df=1 no
    docs = []
    for i in range(10):
        t = []
        if i < 4: t.append("cuatro")
        if i < 5: t.append("cinco")
        if i < 2: t.append("dos")
        if i < 1: t.append("uno")
        t.append(f"relleno{i:02d}x")
        alm.guardar_texto(a, "30111111112", str(i + 1), " ".join(t))
        docs.append({"id": str(i + 1), "t": "hechos", "texto_ok": True})
    alm.guardar_indice_empresa(a, "30111111112", docs)
    bus.construir(a, [{"empresa": "E", "cuit": "30111111112"}], ahora=AHORA)
    todos = {}
    for k in range(64):
        todos.update(gz(a, bus.nombre_shard(k)))
    assert set(todos) == {"cuatro", "dos"}


def test_solo_los_tipos_y_documentos_indexables(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    c = "30111111112"
    docs = [{"id": "1", "t": "hechos", "texto_ok": True}, {"id": "2", "t": "estados_contables", "texto_ok": True},
            {"id": "3", "t": "hechos", "texto_ok": False}, {"id": "4", "t": "otros", "texto_ok": True}, {"id": "5", "t": "actas", "texto_ok": True},
            {"id": "6", "t": "hechos", "texto_ok": True}, {"id": "7", "t": "prospectos", "texto_ok": True}]
    alm.guardar_indice_empresa(a, c, docs)
    for i in "123457":
        alm.guardar_texto(a, c, i, f"palabraunica{i} comparte tema")
    alm.guardar_texto(a, c, "6", "[pagina 1]\n  ")    # sin tokens utiles: no entra
    # el documento 6 tiene texto pero ninguno util; el 8 esta en el indice sin texto en el almacen
    assert bus.documentos_indexables(a, [{"empresa": "E", "cuit": c}]) == [(c, "1"), (c, "5"), (c, "6"), (c, "7")]
    r = bus.construir(a, [{"empresa": "E", "cuit": c}], ahora=AHORA)
    assert r["n_docs"] == 3 and [d[1] for d in gz(a, "cnv/busqueda/docs.json.gz")] == ["1", "5", "7"]
    assert bus.documentos_indexables(a, []) == []


def test_solo_los_primeros_20000_caracteres_y_sin_marcas_de_pagina(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    c = "30111111112"
    relleno = ("zzz " * 5000)[:19_990]
    for i in ("1", "2"):
        alm.guardar_texto(a, c, i, f"[pagina 1]\n{relleno} dentro {'x' * 20_000} fuera")      # 'dentro' antes del corte, 'fuera' despues
    for i in ("3", "4", "5"):
        alm.guardar_texto(a, c, i, f"[pagina 1]\notro{i}x texto distinto{i}x")                # 5 docs: df=2 es 40% y entra
    alm.guardar_indice_empresa(a, c, [{"id": str(i), "t": "hechos", "texto_ok": True} for i in range(1, 6)])
    bus.construir(a, [{"empresa": "E", "cuit": c}], ahora=AHORA)
    todos = {}
    for k in range(64):
        todos.update(gz(a, bus.nombre_shard(k)))
    assert "zzz" in todos and "dentro" in todos and "fuera" not in todos and "pagina" not in todos


def test_documentos_sin_texto_en_el_almacen_se_omiten(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, "30111111112", [{"id": "1", "t": "hechos", "texto_ok": True}])
    r = bus.construir(a, [{"empresa": "E", "cuit": "30111111112"}], ahora=AHORA)
    assert r["n_docs"] == 0 and gz(a, "cnv/busqueda/docs.json.gz") == [] and json.loads(a.leer("cnv/busqueda/meta.json"))["avgdl"] == 0
    assert all(gz(a, bus.nombre_shard(k)) == {} for k in range(64))      # los 64 archivos existen aunque esten vacios


# --- idempotencia y tiempo -----------------------------------------------------------------

def test_idempotente_mismos_bytes_y_sin_reescribir(base):
    a, universo = base
    bus.construir(a, universo, ahora=AHORA)
    nombres = ["cnv/busqueda/meta.json", "cnv/busqueda/docs.json.gz"] + [bus.nombre_shard(k) for k in range(64)]
    antes = {n: a.leer(n) for n in nombres}
    r = bus.construir(a, universo, ahora=AHORA + dt.timedelta(days=1), forzar=True)      # forzado: reconstruye y aun asi no reescribe nada
    assert r["escritos"] == 0 and {n: a.leer(n) for n in nombres} == antes        # ni siquiera cambia la fecha "generado"
    assert bus.construir(a, universo, ahora=AHORA + dt.timedelta(days=1)) == {"estado": "ok", "sin_cambios": True, "n_docs": 20}      # y sin forzar ni lo intenta


def editar_texto(a, cuit, doc, texto):
    """Como el flujo real: al bajar de nuevo un documento cambian el texto Y los datos del indice por empresa (`n`, `p`)."""
    alm.guardar_texto(a, cuit, doc, texto)
    ind = alm.leer_indice_empresa(a, cuit)
    for m in ind:
        if m["id"] == doc:
            m["n"] = len(texto)
    alm.guardar_indice_empresa(a, cuit, ind)


def test_si_cambia_el_corpus_se_reescribe_y_se_actualiza_la_fecha(base):
    a, universo = base
    bus.construir(a, universo, ahora=AHORA)
    editar_texto(a, "30111111112", "101", "[pagina 1]\ntexto completamente distinto recompra obligaciones")
    r = bus.construir(a, universo, ahora=AHORA + dt.timedelta(days=1))
    assert r["escritos"] > 0 and json.loads(a.leer("cnv/busqueda/meta.json"))["generado"] == "2026-10-17T12:00:00Z"


def test_gzip_deterministico():
    assert bus._gz({"a": [[1, 2]]}) == bus._gz({"a": [[1, 2]]}) and gzip.decompress(bus._gz({"ñ": 1})) == '{"ñ":1}'.encode("utf-8")


def test_sin_tiempo_no_escribe_nada_y_deja_el_indice_anterior(base):
    a, universo = base
    bus.construir(a, universo, ahora=AHORA)
    antes = a.leer("cnv/busqueda/meta.json"), a.leer("cnv/busqueda/docs.json.gz")
    editar_texto(a, "30111111112", "101", "[pagina 1]\notro texto recompra obligaciones")
    r = bus.construir(a, universo, hasta_t=1.0, ahora=AHORA)
    assert r["estado"] == "sin_tiempo" and (a.leer("cnv/busqueda/meta.json"), a.leer("cnv/busqueda/docs.json.gz")) == antes


def test_lotes_chicos_dan_el_mismo_indice(base):
    a, universo = base
    bus.construir(a, universo, ahora=AHORA, lote=3, trabajadores=4)
    chico = {n: a.leer(n) for n in ["cnv/busqueda/docs.json.gz", bus.nombre_shard(bus.shard_de("recompra"))]}
    bus.construir(a, universo, ahora=AHORA, lote=1000, trabajadores=1)
    assert {n: a.leer(n) for n in chico} == chico


# --- oraculo BM25 ---------------------------------------------------------------------------

def test_el_fixture_del_oraculo_esta_al_dia():
    """Si falla: cambio el formato o el algoritmo. Regenerar con `python -m tests.generar_oraculo`, subir bus.VERSION y avisar al lado JS."""
    assert generar_oraculo.generar() == ORACULO


def test_bm25_reproduce_las_3_consultas_del_fixture(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    por_cuit = {}
    for x in ORACULO["corpus"]:
        alm.guardar_texto(a, x["cuit"], x["doc_id"], x["texto"])
        por_cuit.setdefault(x["cuit"], []).append({"id": x["doc_id"], "t": x["tipo"], "texto_ok": True})
    for cuit, docs in por_cuit.items():
        alm.guardar_indice_empresa(a, cuit, docs)
    bus.construir(a, [{"empresa": c, "cuit": c} for c in por_cuit], ahora=AHORA)
    assert len(ORACULO["consultas"]) == 3
    for c in ORACULO["consultas"]:
        obtenido = [[cu, d, round(s, 6)] for cu, d, s in bus.buscar_bm25(a, c["consulta"], c["k"])]
        assert obtenido == c["esperado"] and len(obtenido) >= 3
    # el indice escrito coincide con el del fixture, termino por termino
    for nombre, esperado in ORACULO["indice"]["shards"].items():
        assert gz(a, f"cnv/busqueda/{nombre}.json.gz") == esperado
    assert gz(a, "cnv/busqueda/docs.json.gz") == ORACULO["indice"]["docs"]


def test_bm25_un_termino_coincide_con_la_formula(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    for i, texto in enumerate(["rara palabra extra", "rara rara palabra", "cosa distinta nueva", "algo nuevo ahora", "texto breve aqui"], 1):
        alm.guardar_texto(a, "30111111112", str(i), texto)
    alm.guardar_indice_empresa(a, "30111111112", [{"id": str(i), "t": "hechos", "texto_ok": True} for i in range(1, 6)])
    bus.construir(a, [{"empresa": "E", "cuit": "30111111112"}], ahora=AHORA)
    n, avgdl, df = 5, 3.0, 2        # cada doc tiene 3 tokens utiles
    idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
    f = lambda tf: idf * tf * 2.2 / (tf + 1.2 * (1 - 0.75 + 0.75 * 3 / avgdl))  # noqa: E731
    r = bus.buscar_bm25(a, "RARA", 10)
    assert [(c, d) for c, d, _ in r] == [("30111111112", "2"), ("30111111112", "1")]
    assert r[0][2] == pytest.approx(f(2)) and r[1][2] == pytest.approx(f(1))
    assert len(bus.buscar_bm25(a, "rara", 1)) == 1


def test_bm25_casos_borde(base, tmp_path):
    a, universo = base
    assert bus.buscar_bm25(alm.AlmacenLocal(str(tmp_path / "vacio")), "recompra") == []       # sin indice
    bus.construir(a, universo, ahora=AHORA)
    assert bus.buscar_bm25(a, "") == [] and bus.buscar_bm25(a, "de la el") == [] and bus.buscar_bm25(a, "inexistente") == []
    assert bus.buscar_bm25(a, "comunicado") == []          # termino demasiado comun: no esta en el indice
    assert bus.buscar_bm25(a, "recompra recompra recompra", 10) == bus.buscar_bm25(a, "recompra", 10)   # terminos repetidos cuentan una vez
    a.guardar("cnv/busqueda/meta.json", json.dumps({"version": 99}).encode())
    assert bus.buscar_bm25(a, "recompra") == []            # version desconocida


# --- linea de comandos ------------------------------------------------------------------------

def test_cli_busqueda(base, monkeypatch, capsys, tmp_path):
    a, universo = base
    monkeypatch.setenv("DATOS_LOCAL", str(a.raiz))
    monkeypatch.setattr(cli, "cargar_universo", lambda: universo)
    assert cli.main(["busqueda"]) == 0 and "'estado': 'ok'" in capsys.readouterr().out
    assert cli.main(["busqueda", "--tiempo-max", "-1"]) == 0 and "sin_cambios" in capsys.readouterr().out      # nada cambio: no necesita tiempo
    assert cli.main(["busqueda", "--tiempo-max", "-1", "--forzar"]) == 3 and "sin tiempo" in capsys.readouterr().out


# --- Fase 23: huella del corpus ---------------------------------------------------------------

def test_la_huella_no_cambia_si_solo_cambian_el_resumen_o_la_categoria(base):
    a, universo = base
    antes = bus.huella_corpus(a, universo)
    ind = alm.leer_indice_empresa(a, "30111111112")
    for m in ind:
        m["r"], m["k"], m["kv"] = "un resumen nuevo", "dividendos", "2"
    alm.guardar_indice_empresa(a, "30111111112", ind)
    assert bus.huella_corpus(a, universo) == antes


@pytest.mark.parametrize("cambio", [{"n": 99999}, {"p": 77}, {"trunc": {"motivo": "paginas"}}, {"ocr": 3}, {"parcial": 1}, {"texto_ok": False}, {"t": "otros"}])
def test_la_huella_cambia_si_cambia_lo_que_describe_el_texto(base, cambio):
    a, universo = base
    antes = bus.huella_corpus(a, universo)
    ind = alm.leer_indice_empresa(a, "30111111112")
    ind[0].update(cambio)
    alm.guardar_indice_empresa(a, "30111111112", ind)
    assert bus.huella_corpus(a, universo) != antes


def test_la_huella_cambia_si_cambian_las_reglas_de_tokenizacion(base, monkeypatch):
    a, universo = base
    antes = bus.huella_corpus(a, universo)
    monkeypatch.setattr(bus, "STOPWORDS", bus.STOPWORDS | {"palabranueva"})
    assert bus.huella_corpus(a, universo) != antes
    monkeypatch.setattr(bus, "STOPWORDS", frozenset(bus.STOPWORDS - {"palabranueva"}))
    monkeypatch.setattr(bus, "MAX_CARACTERES", 100)
    assert bus.huella_corpus(a, universo) != antes


def test_sin_huella_guardada_o_ilegible_reconstruye(base):
    a, universo = base
    bus.construir(a, universo, ahora=AHORA)
    a.guardar(bus.RUTA_HUELLA, b"no es json")
    assert bus.construir(a, universo, ahora=AHORA).get("sin_cambios") is None
    assert json.loads(a.leer(bus.RUTA_HUELLA))["huella"] == bus.huella_corpus(a, universo)      # y la deja bien guardada
    a2 = alm.AlmacenLocal(a.raiz + "_otra")
    corpus_20(a2)
    assert bus.construir(a2, universo, ahora=AHORA).get("estado") == "ok"          # primera vez: construye aunque no haya huella


def test_sin_meta_no_se_saltea_aunque_la_huella_coincida(base):
    a, universo = base
    bus.construir(a, universo, ahora=AHORA)
    os.remove(os.path.join(a.raiz, "cnv", "busqueda", "meta.json"))
    assert bus.construir(a, universo, ahora=AHORA).get("sin_cambios") is None and a.existe("cnv/busqueda/meta.json")
