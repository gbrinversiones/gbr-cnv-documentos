"""Sincronizacion incremental de documentos de la CNV: lista, baja lo que falta, extrae texto y actualiza los indices."""
import csv
import datetime as dt
import gzip
import json
import time
from concurrent.futures import ThreadPoolExecutor

from . import almacen as alm
from .descarga import texto_de_presentacion
from .listado import listar, sesion

TIPOS_DEFECTO = ("hechos",)


def cargar_universo(ruta: str = "data/universo.csv") -> list:
    with open(ruta, encoding="utf-8", newline="") as f:
        return [{"empresa": r["empresa"], "cuit": r["cuit"].replace("-", "")} for r in csv.DictReader(f) if r.get("cuit")]


def _con_reintentos(fn, intentos: int = 3, espera: float = 4.0):
    ultimo = None
    for n in range(intentos):
        try:
            return fn()
        except Exception as error:  # la CNV responde lento o corta a veces: se reintenta con espera creciente
            ultimo = error
            time.sleep(espera * (n + 1))
    raise ultimo


def pendientes(docs: list, indice: list, tipos: tuple) -> list:
    """Documentos del listado de los tipos pedidos que todavia no tienen texto guardado ni intento terminado."""
    hechos = {d["id"]: d for d in indice}
    salida = []
    for d in docs:
        if d["tipo"] not in tipos:
            continue
        previo = hechos.get(d["doc_id"])
        if previo and (previo.get("texto_ok") or previo.get("intentos", 0) >= 3):
            continue
        salida.append(d)
    return salida


def _procesar(almacen, empresa: dict, d: dict, previo: dict | None) -> dict:
    meta = {
        "id": d["doc_id"], "c": empresa["cuit"], "e": empresa["empresa"], "f": d["fecha"], "t": d["tipo"],
        "s": d["seccion"], "d": d["descripcion"][:300], "u": d["url"], "intentos": (previo or {}).get("intentos", 0),
    }
    try:
        r = _con_reintentos(lambda: texto_de_presentacion(d["url"], sesion()))
    except Exception as error:
        meta.update({"texto_ok": False, "intentos": meta["intentos"] + 1, "error": str(error)[:120]})
        return meta
    if not r or not r.get("texto", "").strip():
        meta.update({"texto_ok": False, "intentos": meta["intentos"] + 1, "error": "sin texto" if r else "sin adjunto", "escaneado": bool(r and r.get("escaneado"))})
        return meta
    alm.guardar_texto(almacen, empresa["cuit"], d["doc_id"], r["texto"])
    meta.update({"texto_ok": True, "p": r["paginas"], "n": len(r["texto"]), "archivos": r["archivos"][:3]})
    return meta


def sincronizar_empresa(almacen, empresa: dict, desde: dt.date, tipos: tuple = TIPOS_DEFECTO, trabajadores: int = 3, max_docs: int | None = None, hasta_t: float | None = None) -> dict:
    """Baja los documentos nuevos de una empresa. Devuelve {"nuevos","fallidos","pendientes"}."""
    docs = _con_reintentos(lambda: listar(empresa["cuit"], desde))
    indice = alm.leer_indice_empresa(almacen, empresa["cuit"])
    por_id = {d["id"]: d for d in indice}
    # el listado de hoy tambien alimenta el indice con los documentos de otros tipos (solo metadatos, sin texto)
    for d in docs:
        por_id.setdefault(d["doc_id"], {"id": d["doc_id"], "c": empresa["cuit"], "e": empresa["empresa"], "f": d["fecha"], "t": d["tipo"],
                                         "s": d["seccion"], "d": d["descripcion"][:300], "u": d["url"], "texto_ok": False, "intentos": 0})
    faltan = pendientes(docs, list(por_id.values()), tipos)
    if max_docs is not None:
        faltan = faltan[:max_docs]
    nuevos = fallidos = 0
    if faltan:
        with ThreadPoolExecutor(max_workers=trabajadores) as pool:
            def tarea(d):
                if hasta_t and time.monotonic() > hasta_t:
                    return None
                return _procesar(almacen, empresa, d, por_id.get(d["doc_id"]))
            for meta in pool.map(tarea, faltan):
                if meta is None:
                    continue
                por_id[meta["id"]] = {**por_id.get(meta["id"], {}), **meta}
                nuevos += 1 if meta.get("texto_ok") else 0
                fallidos += 0 if meta.get("texto_ok") else 1
    alm.guardar_indice_empresa(almacen, empresa["cuit"], sorted(por_id.values(), key=lambda m: int(m["id"]), reverse=True))
    return {"nuevos": nuevos, "fallidos": fallidos, "pendientes": max(0, len(faltan) - nuevos - fallidos)}


def construir_indice_global(almacen, empresas: list) -> int:
    """Junta los indices por empresa en cnv/indice/global.json.gz (solo documentos con texto)."""
    filas = []
    for e in empresas:
        for m in alm.leer_indice_empresa(almacen, e["cuit"]):
            if m.get("texto_ok"):
                filas.append({k: m.get(k) for k in ("id", "c", "e", "f", "t", "s", "d", "u", "p", "n", "r")})
    filas.sort(key=lambda m: (m["f"] or "", m["id"]), reverse=True)
    almacen.guardar("cnv/indice/global.json.gz", gzip.compress(json.dumps(filas, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
    return len(filas)
