"""Sincronizacion incremental de documentos de la CNV: lista, baja lo que falta, extrae texto y actualiza los indices."""
import csv
import datetime as dt
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

from . import almacen as alm
from . import avisos_pago, ocr
from .descarga import Limites, extraer_pdf, pdfs_de_presentacion, texto_de_presentacion
from .listado import listar, sesion

# Tipos de los que se baja el texto. "otros" (y cualquier seccion nueva de la CNV) queda solo como metadato en el indice.
TIPOS_TEXTO = ("hechos", "estados_contables", "prospectos", "avisos_pago", "actas")
TIPOS_DEFECTO = TIPOS_TEXTO
MAX_INTENTOS = 3
DESDE_CARGA_INICIAL = dt.date(2023, 1, 1)  # historia que se baja de una empresa que todavia no tiene la suya (ver desde_efectivo)
GUARDAR_CADA = 10  # documentos: el indice se guarda de a tandas para que un corte no obligue a rebajar lo ya bajado


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
            if n < intentos - 1:  # despues del ultimo intento no tiene sentido esperar
                time.sleep(espera * (n + 1))
    raise ultimo


def desde_efectivo(indice: list, desde: dt.date, tipos: tuple) -> dt.date:
    """Fecha desde la que se lista a la empresa. El sync diario mira solo los ultimos dias; una empresa recien agregada al universo (o
    sin ningun estado contable en el indice) nunca tuvo su carga inicial y, si no publico nada en esa ventana, quedaba con el indice
    vacio o sin balances para siempre. Todas las empresas presentan estados contables, asi que ninguno en el indice (con o sin texto)
    significa que falta la historia: se lista desde DESDE_CARGA_INICIAL. Lo ya bajado no se repite (ver `pendientes`)."""
    if not indice or ("estados_contables" in tipos and not any(m.get("t") == "estados_contables" for m in indice)):
        return min(desde, DESDE_CARGA_INICIAL)
    return desde


def pendientes(docs: list, indice: list, tipos: tuple) -> list:
    """Documentos del listado de los tipos pedidos que todavia no tienen texto guardado ni intento terminado.
    Los escaneados que fallaron NO se reintentan aca: con OCR se reprocesan aparte (`reprocesar_escaneados`)."""
    hechos = {d["id"]: d for d in indice}
    salida = []
    for d in docs:
        if d["tipo"] not in tipos:
            continue
        previo = hechos.get(d["doc_id"])
        if previo and previo.get("intentos", 0) >= MAX_INTENTOS:
            continue
        if previo and previo.get("texto_ok") and not previo.get("parcial"):
            continue  # un resultado parcial (algun adjunto fallo) se reintenta hasta agotar los intentos
        salida.append(d)
    return salida


# Orden en que se atiende lo pendiente: primero lo que mueve cifras y avisos (estados contables), despues hechos y avisos de pago, al final el volumen
# (prospectos, actas). Cada grupo es una pasada completa sobre las empresas (ver `grupos_de_tipos` y cnv/__main__.py).
GRUPOS_PRIORIDAD = (("estados_contables",), ("hechos", "avisos_pago"), ("prospectos",), ("actas",))
GRUPOS_CON_TOPE = 2  # a partir de este grupo (prospectos, actas) la pasada tiene un tope de tiempo propio: no desplaza a los de arriba


def grupos_de_tipos(tipos: tuple) -> list:
    """Los `tipos` pedidos repartidos en GRUPOS_PRIORIDAD (los que no figuran en ningun grupo van al final, juntos)."""
    grupos = [tuple(t for t in g if t in tipos) for g in GRUPOS_PRIORIDAD]
    resto = tuple(t for t in tipos if not any(t in g for g in GRUPOS_PRIORIDAD))
    return [g for g in [*grupos, resto] if g]


def pendientes_del_indice(indice: list, ya_listados: set, tipos: tuple) -> list:
    """Documentos que el indice conoce pero nunca terminaron de bajarse y YA NO salen en el listado de la ventana pedida: el sync diario solo
    lista los ultimos dias, asi que lo que quedo sin bajar (corte por tiempo, tipos que no se pidieron esa vez, intento fallido) no se
    reintentaba nunca. Se rearman con los datos guardados (incluida la URL), en la forma que devuelve `listar`."""
    salida = []
    for m in indice:
        if m["id"] in ya_listados or m.get("t") not in tipos or not m.get("u"):
            continue
        if m.get("intentos", 0) >= MAX_INTENTOS or (m.get("texto_ok") and not m.get("parcial")):
            continue
        salida.append({"doc_id": m["id"], "fecha": m.get("f") or "", "seccion": m.get("s", ""), "tipo": m["t"], "descripcion": m.get("d", ""), "url": m["u"]})
    return salida


def _procesar(almacen, empresa: dict, d: dict, previo: dict | None, limites: Limites | None = None, hasta_t: float | None = None) -> dict:
    meta = {
        "id": d["doc_id"], "c": empresa["cuit"], "e": empresa["empresa"], "f": d["fecha"], "t": d["tipo"],
        "s": d["seccion"], "d": d["descripcion"][:300], "u": d["url"], "intentos": (previo or {}).get("intentos", 0),
    }
    try:
        r = _con_reintentos(lambda: texto_de_presentacion(d["url"], sesion(), limites=limites, hasta_t=hasta_t))
    except Exception as error:
        meta.update({"texto_ok": False, "intentos": meta["intentos"] + 1, "error": str(error)[:120]})
        return meta
    fallos = (r or {}).get("fallos") or []
    if not r or not r.get("texto", "").strip():
        motivo = "; ".join(f"{f['archivo']}: {f['motivo']}" for f in fallos)[:120] or ("sin texto" if r else "sin adjunto")
        meta.update({"texto_ok": False, "intentos": meta["intentos"] + 1, "error": motivo, "escaneado": bool(r and r.get("escaneado"))})
        if r and r.get("ocr_intentado"):
            meta["ocr_intentado"] = True  # el OCR corrio y no saco texto: no se vuelve a gastar tiempo en este documento
        return meta
    meta["intentos"] += 1 if fallos else 0
    if fallos and previo and previo.get("texto_ok") and (previo.get("n") or 0) >= len(r["texto"]):
        # reintento de un parcial que no mejoro: se conserva el texto que ya estaba guardado
        return {**previo, "intentos": meta["intentos"], "error": "; ".join(f"{f['archivo']}: {f['motivo']}" for f in fallos)[:120]}
    alm.guardar_texto(almacen, empresa["cuit"], d["doc_id"], r["texto"])
    meta.update({"texto_ok": True, "p": r["paginas"], "n": len(r["texto"]), "archivos": r["archivos"][:3]})
    # campos opcionales: solo existen cuando hay algo que contar, asi los indices viejos no cambian de forma
    if r.get("truncado"):
        meta["trunc"] = r["truncado"]
    if r.get("ocr"):
        meta["ocr"] = r["ocr"]  # paginas leidas por OCR: texto menos confiable (ver cnv/ocr.py)
    if r.get("serie"):
        meta["serie"] = r["serie"]  # metadata de la CNV (clase/serie); respaldo para los avisos de pago
    if r.get("escaneado"):
        meta["escaneado"] = True  # se guarda igual (como antes), pero marcado: sirve para el OCR y para el informe de salud
        if r.get("ocr_intentado"):
            meta["ocr_intentado"] = True
    if fallos:
        meta["parcial"] = len(fallos)
        meta["error"] = "; ".join(f"{f['archivo']}: {f['motivo']}" for f in fallos)[:120]
    return meta


_VOLATILES = ("error", "parcial", "trunc", "escaneado", "ocr", "ocr_intentado")


def _fusionar(previo: dict | None, meta: dict) -> dict:
    """Mezcla el intento nuevo sobre lo que habia. Si el intento nuevo tuvo texto, los rastros de intentos anteriores
    (error, parcial, truncado) se descartan: el resultado nuevo es el que vale."""
    base = dict(previo or {})
    if meta.get("texto_ok"):
        for k in _VOLATILES:
            base.pop(k, None)
    return {**base, **meta}


def sincronizar_empresa(almacen, empresa: dict, desde: dt.date, tipos: tuple = TIPOS_DEFECTO, trabajadores: int = 3, max_docs: int | None = None,
                        hasta_t: float | None = None, limites: Limites | None = None, con_listado: bool = True) -> dict:
    """Baja los documentos nuevos de una empresa, y los que el indice ya conoce y quedaron sin bajar. Devuelve {"nuevos","fallidos","pendientes"}.
    `con_listado=False` salta la consulta a la CNV y atiende solo lo pendiente del indice (pasadas siguientes a la primera)."""
    indice = alm.leer_indice_empresa(almacen, empresa["cuit"])
    desde = desde_efectivo(indice, desde, tipos)
    docs = _con_reintentos(lambda: listar(empresa["cuit"], desde)) if con_listado else []
    por_id = {d["id"]: d for d in indice}
    # el listado de hoy tambien alimenta el indice con los documentos de otros tipos (solo metadatos, sin texto)
    for d in docs:
        por_id.setdefault(d["doc_id"], {"id": d["doc_id"], "c": empresa["cuit"], "e": empresa["empresa"], "f": d["fecha"], "t": d["tipo"],
                                         "s": d["seccion"], "d": d["descripcion"][:300], "u": d["url"], "texto_ok": False, "intentos": 0})
    faltan = pendientes(docs, list(por_id.values()), tipos)
    faltan += pendientes_del_indice(list(por_id.values()), {d["doc_id"] for d in docs}, tipos)
    faltan.sort(key=lambda d: (tipos.index(d["tipo"]) if d["tipo"] in tipos else len(tipos), -int(d["doc_id"])))  # el orden de `tipos` es la prioridad; dentro, los mas nuevos
    if max_docs is not None:
        faltan = faltan[:max_docs]
    nuevos = fallidos = 0

    def guardar():
        alm.guardar_indice_empresa(almacen, empresa["cuit"], sorted(por_id.values(), key=lambda m: int(m["id"]), reverse=True))

    try:
        if faltan:
            with ThreadPoolExecutor(max_workers=trabajadores) as pool:
                def tarea(d):
                    if hasta_t and time.monotonic() > hasta_t:
                        return None
                    return _procesar(almacen, empresa, d, por_id.get(d["doc_id"]), limites, hasta_t)
                for meta in pool.map(tarea, faltan):
                    if meta is None:
                        continue
                    por_id[meta["id"]] = _fusionar(por_id.get(meta["id"]), meta)
                    nuevos += 1 if meta.get("texto_ok") else 0
                    fallidos += 0 if meta.get("texto_ok") else 1
                    if (nuevos + fallidos) % GUARDAR_CADA == 0:
                        guardar()  # un corte (timeout del job, red) no pierde lo ya bajado
    finally:
        guardar()
    return {"nuevos": nuevos, "fallidos": fallidos, "pendientes": max(0, len(faltan) - nuevos - fallidos)}


def reprocesar_escaneados(almacen, empresa: dict, limites: Limites | None = None, hasta_t: float | None = None, max_docs: int | None = None) -> dict:
    """Vuelve a bajar los documentos que fallaron por ser imagen escaneada y les aplica OCR (requiere CNV_OCR=1).
    Usa la URL guardada en el indice: no vuelve a consultar el listado de la CNV. Devuelve {"ocr","sin_texto","pendientes"}."""
    if not ocr.activado():
        return {"ocr": 0, "sin_texto": 0, "pendientes": 0, "error": "CNV_OCR no esta activado"}
    try:
        ocr.comprobar(ocr.configuracion()["idioma"])
    except ocr.OcrNoDisponible as error:  # sin tesseract no se toca nada: los documentos quedan para una corrida con el entorno completo
        return {"ocr": 0, "sin_texto": 0, "pendientes": 0, "error": str(error)}
    indice = alm.leer_indice_empresa(almacen, empresa["cuit"])
    por_id = {m["id"]: m for m in indice}
    cola = [m for m in indice if m.get("escaneado") and not m.get("ocr") and not m.get("ocr_intentado") and m.get("t") in TIPOS_TEXTO]
    if max_docs is not None:
        cola = cola[:max_docs]
    hechos = sin_texto = 0
    try:
        for m in cola:
            if hasta_t and time.monotonic() > hasta_t:
                break
            d = {"doc_id": m["id"], "fecha": m.get("f"), "tipo": m["t"], "seccion": m.get("s", ""), "descripcion": m.get("d", ""), "url": m["u"]}
            nuevo = _procesar(almacen, empresa, d, m, limites, hasta_t)
            por_id[m["id"]] = _fusionar(m, nuevo)
            leido = bool(nuevo.get("ocr"))
            hechos += leido
            sin_texto += not leido
            if (hechos + sin_texto) % GUARDAR_CADA == 0:
                alm.guardar_indice_empresa(almacen, empresa["cuit"], sorted(por_id.values(), key=lambda x: int(x["id"]), reverse=True))
    finally:
        alm.guardar_indice_empresa(almacen, empresa["cuit"], sorted(por_id.values(), key=lambda x: int(x["id"]), reverse=True))
    return {"ocr": hechos, "sin_texto": sin_texto, "pendientes": len(cola) - hechos - sin_texto}


def ocr_formularios(almacen, empresa: dict, limites: Limites | None = None, hasta_t: float | None = None, max_docs: int | None = None) -> dict:
    """Avisos de pago cuyo PDF es un formulario sin valores en el texto (ver avisos_pago.es_formulario_sin_valores): vuelve a bajar el PDF,
    renderiza cada pagina y la lee con tesseract descartando los numeros poco confiables. El resultado va a una ruta NUEVA
    (cnv/texto/{cuit}/{doc_id}.ocr.txt.gz); el texto original no se toca. Devuelve {"leidos","sin_texto","pendientes"} o {"error": ...}."""
    if not ocr.activado():
        return {"leidos": 0, "sin_texto": 0, "pendientes": 0, "error": "CNV_OCR no esta activado"}
    cfg = ocr.configuracion()
    try:
        ocr.comprobar(cfg["idioma"])
    except ocr.OcrNoDisponible as error:
        return {"leidos": 0, "sin_texto": 0, "pendientes": 0, "error": str(error)}
    indice = alm.leer_indice_empresa(almacen, empresa["cuit"])
    por_id = {m["id"]: m for m in indice}
    cola = []
    for m in indice:
        if m.get("t") != "avisos_pago" or not m.get("texto_ok") or m.get("ocr_form") or m.get("ocr_form_intentado") or not m.get("u"):
            continue
        if avisos_pago.es_formulario_sin_valores(alm.leer_texto(almacen, empresa["cuit"], m["id"]) or ""):
            cola.append(m)
    if max_docs is not None:
        cola = cola[:max_docs]
    leidos = sin_texto = 0

    def guardar():
        alm.guardar_indice_empresa(almacen, empresa["cuit"], sorted(por_id.values(), key=lambda x: int(x["id"]), reverse=True))

    try:
        for m in cola:
            if hasta_t and time.monotonic() > hasta_t:
                break
            try:
                pdfs = _con_reintentos(lambda: pdfs_de_presentacion(m["u"], sesion(), limites, hasta_t), intentos=2)["pdfs"]
            except Exception:  # la CNV no respondio: no se marca como intentado, se reintenta en la proxima corrida
                continue
            partes, paginas, ilegibles, numericas = [], 0, 0, 0
            for nombre, pdf in pdfs:
                total = extraer_pdf(pdf, 1)["paginas"]
                o = ocr.ocr_pdf_confiable(pdf, total, cfg["max_paginas"], cfg["idioma"], max(cfg["dpi"], 300), ocr.confianza_minima())
                partes.append(o["texto"])
                paginas, numericas, ilegibles = paginas + o["paginas_ocr"], numericas + o["numericas"], ilegibles + o["ilegibles"]
            texto = "\n\n".join(partes)
            nuevo = dict(m)
            if numericas - ilegibles > 0:  # al menos un numero leido con confianza: hay algo que extraer
                alm.guardar_texto_ocr(almacen, empresa["cuit"], m["id"], texto)
                nuevo.update({"ocr_form": paginas, "ocr_form_numeros": numericas - ilegibles, "ocr_form_ilegibles": ilegibles})
                leidos += 1
            else:
                nuevo.update({"ocr_form_intentado": True, "ocr_form_ilegibles": ilegibles})
                sin_texto += 1
            por_id[m["id"]] = nuevo
            if (leidos + sin_texto) % GUARDAR_CADA == 0:
                guardar()
    finally:
        guardar()
    return {"leidos": leidos, "sin_texto": sin_texto, "pendientes": len(cola) - leidos - sin_texto}


RUTA_FIRMA_GLOBAL = "cnv/indice/global.firma.json"


def firma_indices(almacen, empresas: list) -> str:
    """Huella de los indices por empresa (cnv/indice/{cuit}.json) de `empresas`: si no cambia, el indice global tampoco. Un solo listado, no descarga nada."""
    firmas = almacen.firmas("cnv/indice/")
    partes = []
    for e in sorted(empresas, key=lambda e: e["cuit"]):
        partes.append(e["cuit"] + "=" + firmas.get("cnv/indice/" + e["cuit"] + ".json", "-"))
    return hashlib.sha1("\n".join(partes).encode("utf-8")).hexdigest()


def construir_indice_global(almacen, empresas: list, forzar: bool = False) -> int:
    """Junta los indices por empresa en cnv/indice/global.json.gz (solo documentos con texto).
    Si ningun indice por empresa cambio desde la ultima vez (huella en cnv/indice/global.firma.json) y el global existe, no lo reconstruye: devuelve la
    cantidad guardada. `forzar` lo reconstruye igual."""
    firma = firma_indices(almacen, empresas)
    previa = almacen.leer(RUTA_FIRMA_GLOBAL)
    if not forzar and previa and almacen.existe("cnv/indice/global.json.gz"):
        try:
            d = json.loads(previa)
            if d.get("firma") == firma and isinstance(d.get("n"), int):
                return d["n"]
        except ValueError:
            pass
    filas = []
    for e in empresas:
        for m in alm.leer_indice_empresa(almacen, e["cuit"]):
            if m.get("texto_ok"):
                fila = {k: m.get(k) for k in ("id", "c", "e", "f", "t", "s", "d", "u", "p", "n", "r", "k")}
                if m.get("trunc"):  # solo cuando existe: avisa al buscador que el texto no cubre todo el documento
                    fila["tr"] = m["trunc"]["motivo"]
                if m.get("ocr"):  # texto reconocido por OCR: puede tener errores de lectura
                    fila["oc"] = m["ocr"]
                filas.append(fila)
    filas.sort(key=lambda m: (m["f"] or "", m["id"]), reverse=True)
    alm.guardar_si_cambio(almacen, "cnv/indice/global.json.gz", alm.comprimir(json.dumps(filas, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
    alm.guardar_si_cambio(almacen, RUTA_FIRMA_GLOBAL, json.dumps({"firma": firma, "n": len(filas)}, separators=(",", ":")).encode("utf-8"))
    return len(filas)
