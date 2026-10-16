"""Indice de busqueda de TEXTO COMPLETO (BM25) para encontrar documentos por lo que dicen y no solo por el titulo.

Este modulo ESCRIBE el indice; el buscador de la landing (una Cloud Function en Node) lo LEE con una implementacion
identica de la tokenizacion. El formato es un contrato entre los dos lados: no cambiarlo sin subir `version`.

Archivos en el bucket (todos nuevos; no toca ninguna ruta existente):
  cnv/busqueda/docs.json.gz        [[cuit, doc_id, longitud_en_tokens], ...]; el indice posicional (doc_idx) es el orden de la lista
  cnv/busqueda/meta.json           {"version": 1, "n_docs": N, "avgdl": promedio, "shards": 64, "generado": "ISO"}
  cnv/busqueda/s00.json.gz ... s63.json.gz   {"termino": [[doc_idx, tf], ...], ...}
      shard de un termino = zlib.crc32(termino.encode("utf-8")) % 64  (crc32 sin signo, el de zlib)
      solo terminos con df >= 2 y df <= 40% de los documentos; tf acotado a 255; postings ordenados por doc_idx

Documentos incluidos: texto_ok y tipo hechos, prospectos, avisos_pago o actas (no estados contables: enormes y de poco valor para
busqueda libre). De cada uno se usan solo los primeros 20.000 caracteres, despues de sacar las marcas "[pagina N]".

Tokenizacion (identica en JS): NFD y sacar marcas combinantes -> minusculas -> tokens = /[a-z0-9]+/ -> descartar largo < 3 o > 30 ->
descartar stopwords (STOPWORDS) -> sin stemming. "longitud_en_tokens" cuenta los tokens que quedan (despues de descartar).

Orden de publicacion: primero los shards, despues docs.json.gz y al final meta.json. El lector arranca por meta.json, asi que un
corte a mitad de la publicacion deja el indice anterior consistente. Si se agota el tiempo ANTES de publicar, no se escribe nada.
"""
import array
import datetime as dt
import gzip
import io
import json
import math
import re
import time
import unicodedata
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import almacen as alm

VERSION = 1
SHARDS = 64
TIPOS_INDEXADOS = ("hechos", "prospectos", "avisos_pago", "actas")
MAX_CARACTERES = 20_000
MIN_TOKEN, MAX_TOKEN = 3, 30
TF_MAX = 255
DF_MIN = 2
DF_MAX_PORCENTAJE = 40
K1, B = 1.2, 0.75

# Lista fija: la usa el lector en JS. Articulos, preposiciones, conjunciones, pronombres, verbos auxiliares y palabras vacias
# propias de los documentos de la CNV ("sociedad", "anonima", "cnv", "empresa", "documento"...). Ya sin acentos y en minuscula.
STOPWORDS = frozenset("""
el la los las lo les un una unos unas uno al del de en con por para sin sobre entre hacia desde hasta segun ante bajo tras durante mediante contra
y e o u ni que como cuando donde cual cuales quien quienes cuyo cuya cuyos cuyas porque pues aunque sino pero mas menos muy tan tanto tanta tantos tantas
se si no ni ya aun tambien solo solamente asi esto esta este estos estas eso esa ese esos esas aquel aquella aquellos aquellas ello
yo tu mi mis tus su sus nos nuestro nuestra nuestros nuestras vos ustedes ellos ellas
es son era eran fue fueron sera seran seria serian sido ser siendo esta estan estaba estaban estuvo estado estar
ha han habia habian hubo haber habido habra he hemos tiene tienen tenia tenian tuvo tener tenido
hace hacen hizo hacer hecho puede pueden podra podran podria poder debe deben debera deberan debido
cada otro otra otros otras mismo misma mismos mismas todo toda todos todas algun alguna algunos algunas ningun ninguna varios varias
muchos muchas pocos pocas demas cualquier cualquiera
sociedad sociedades anonima anonimas empresa empresas compania companias documento documentos cnv comision nacional valores
informa informan informar informamos comunica comunicamos presente presentes nota notas atentamente saludo saludamos
pagina paginas
sra sres senores senor sr
""".split())


# ---------------------------------------------------------------------------
# Tokenizacion y shards
# ---------------------------------------------------------------------------

_RE_TOKEN = re.compile(r"[a-z0-9]+")
_RE_MARCA_PAGINA = re.compile(r"\[pagina \d+\]")


def tokenizar(texto: str) -> list:
    """Tokens de busqueda de un texto (o de una consulta), en orden de aparicion y con repeticiones."""
    t = unicodedata.normalize("NFD", texto or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return [w for w in _RE_TOKEN.findall(t) if MIN_TOKEN <= len(w) <= MAX_TOKEN and w not in STOPWORDS]


def shard_de(termino: str) -> int:
    return zlib.crc32(termino.encode("utf-8")) % SHARDS


def nombre_shard(n: int) -> str:
    return f"cnv/busqueda/s{n:02d}.json.gz"


def _gz(datos) -> bytes:
    """JSON compacto comprimido de forma DETERMINISTA: el mismo contenido da los mismos bytes."""
    return alm.comprimir(json.dumps(datos, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _publicar(almacen, nombre: str, datos: bytes) -> bool:
    """Escribe solo si cambio (ahorra subidas): True si escribio."""
    if almacen.leer(nombre) == datos:
        return False
    almacen.guardar(nombre, datos)
    return True


# ---------------------------------------------------------------------------
# Construccion
# ---------------------------------------------------------------------------

def documentos_indexables(almacen, empresas: list) -> list:
    """[(cuit, doc_id)] de los documentos con texto de los tipos indexados, en orden estable (cuit, id numerico)."""
    salida = []
    for e in empresas:
        for m in alm.leer_indice_empresa(almacen, e["cuit"]):
            if m.get("texto_ok") and m.get("t") in TIPOS_INDEXADOS:
                salida.append((e["cuit"], m["id"]))
    return sorted(set(salida), key=lambda x: (x[0], int(x[1])))


def _contar(almacen, cuit: str, doc_id: str):
    """(longitud, Counter de terminos) o None si el texto no esta o no tiene ningun token util."""
    texto = alm.leer_texto(almacen, cuit, doc_id)
    if not texto:
        return None
    tokens = tokenizar(_RE_MARCA_PAGINA.sub(" ", texto)[:MAX_CARACTERES])
    return (len(tokens), Counter(tokens)) if tokens else None


def construir(almacen, empresas: list, hasta_t: float | None = None, trabajadores: int = 8, ahora: dt.datetime | None = None, lote: int = 256) -> dict:
    """Reconstruye el indice completo. Devuelve {"estado": "ok"|"sin_tiempo", "n_docs", "terminos", "escritos", ...}.
    Con "sin_tiempo" no se escribe NADA (el indice anterior sigue valiendo)."""
    docs_ids = documentos_indexables(almacen, empresas)
    docs, postings = [], {}
    with ThreadPoolExecutor(max_workers=trabajadores) as pool:
        for i in range(0, len(docs_ids), lote):  # por lotes: los textos no se acumulan en memoria
            if hasta_t and time.monotonic() > hasta_t:
                return {"estado": "sin_tiempo", "n_docs": len(docs), "leidos": i, "total": len(docs_ids)}
            parte = docs_ids[i:i + lote]
            for (cuit, doc_id), r in zip(parte, pool.map(lambda d: _contar(almacen, *d), parte)):
                if r is None:
                    continue
                idx = len(docs)
                docs.append([cuit, doc_id, r[0]])
                for termino, tf in r[1].items():
                    postings.setdefault(termino, array.array("Q")).append((idx << 8) | min(tf, TF_MAX))
    n = len(docs)
    tope = n * DF_MAX_PORCENTAJE  # df * 100 <= n * 40  <=>  df <= 40% de los documentos (aritmetica entera: sin errores de redondeo)
    shards = [dict() for _ in range(SHARDS)]
    for termino in sorted(postings):
        p = postings[termino]
        if len(p) < DF_MIN or len(p) * 100 > tope:
            continue
        shards[shard_de(termino)][termino] = [[v >> 8, v & 0xFF] for v in p]
    escritos = 0
    for k in range(SHARDS):
        escritos += _publicar(almacen, nombre_shard(k), _gz(shards[k]))
    escritos += _publicar(almacen, "cnv/busqueda/docs.json.gz", _gz(docs))
    avgdl = round(sum(d[2] for d in docs) / n, 4) if n else 0
    previo = json.loads(almacen.leer("cnv/busqueda/meta.json") or b"{}")
    meta = {"version": VERSION, "n_docs": n, "avgdl": avgdl, "shards": SHARDS, "generado": (ahora or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if {k: v for k, v in previo.items() if k != "generado"} == {k: v for k, v in meta.items() if k != "generado"}:
        meta["generado"] = previo.get("generado", meta["generado"])  # nada cambio: tampoco cambia la fecha (idempotente)
    escritos += _publicar(almacen, "cnv/busqueda/meta.json", json.dumps(meta, separators=(",", ":")).encode("utf-8"))  # al final: es la marca de "indice completo"
    return {"estado": "ok", "n_docs": n, "terminos": sum(len(s) for s in shards), "avgdl": avgdl, "escritos": escritos}


# ---------------------------------------------------------------------------
# Oraculo de referencia (BM25) para el test cruzado con el lector en JS
# ---------------------------------------------------------------------------

def _leer_gz(almacen, nombre: str):
    crudo = almacen.leer(nombre)
    return json.loads(gzip.decompress(crudo)) if crudo else None


def buscar_bm25(almacen, consulta: str, k: int = 10) -> list:
    """[(cuit, doc_id, score)] de los k mejores documentos, leyendo solo los shards de los terminos de la consulta.

    score(d) = sum sobre terminos unicos de la consulta de idf(t) * tf*(K1+1) / (tf + K1*(1 - B + B*len(d)/avgdl))
    idf(t) = ln(1 + (N - df + 0.5) / (df + 0.5)), df = cantidad de postings del termino. Los terminos que no estan en el indice
    (muy raros o muy comunes) no suman. Desempate: menor doc_idx primero. K1 = 1.2, B = 0.75."""
    meta = json.loads(almacen.leer("cnv/busqueda/meta.json") or b"{}")
    if not meta or meta.get("version") != VERSION:
        return []
    docs = _leer_gz(almacen, "cnv/busqueda/docs.json.gz") or []
    n, avgdl = meta["n_docs"], meta["avgdl"] or 1
    puntajes, cache_shards = {}, {}
    for t in dict.fromkeys(tokenizar(consulta)):  # unicos, en orden
        s = shard_de(t)
        if s not in cache_shards:
            cache_shards[s] = _leer_gz(almacen, nombre_shard(s)) or {}
        postings = cache_shards[s].get(t)
        if not postings:
            continue
        df = len(postings)
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for idx, tf in postings:
            largo = docs[idx][2]
            puntajes[idx] = puntajes.get(idx, 0.0) + idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * largo / avgdl))
    mejores = sorted(puntajes.items(), key=lambda x: (-x[1], x[0]))[:k]
    return [(docs[idx][0], docs[idx][1], score) for idx, score in mejores]
