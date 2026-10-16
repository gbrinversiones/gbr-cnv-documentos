"""Descarga del adjunto principal de una presentacion de la CNV y extraccion de su texto.

La CNV es lenta y se corta: cada adjunto se baja con reintentos y espera creciente, con tope de tiempo por
documento, y si un adjunto falla pero otro salio bien se devuelve lo que se pudo (resultado parcial, que el
sincronizador vuelve a intentar mas tarde sin tirar lo ya obtenido).
"""
import html as html_lib
import io
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass

from .listado import sesion

URL_VALET = "https://aif2.cnv.gov.ar/api/ValetKeyProvider/GetPublicValetKey/{guid}?operation=DownloadBlob"
URL_BLOB = "https://blob.cnv.gov.ar/BlobWebService.svc/DownloadBlob/{guid}"
_RE_UPLOADER = re.compile(r'<propiedad[^>]*uploader="[^"]*"[^>]*>(.*?)</propiedad>', re.S | re.I)
_ZIP = b"PK" + bytes([3, 4])

MAX_ADJUNTOS = 3  # algunas presentaciones traen varios adjuntos; mas de 3 es ruido
INTENTOS_ADJUNTO = 4
ESPERA_BASE = 3.0  # segundos; crece 3, 6, 9...
TIEMPO_MAX_DOCUMENTO = 900.0  # tope de reloj por documento (todos sus adjuntos y reintentos)


@dataclass(frozen=True)
class Limites:
    """Topes por documento. Un prospecto puede tener cientos de paginas: se corta y se REGISTRA que se corto."""

    paginas: int = 400
    caracteres: int = 1_500_000
    mb: int = 80  # tamano maximo del archivo descargado

    @classmethod
    def desde_entorno(cls, entorno=None) -> "Limites":
        """CNV_MAX_PAGINAS, CNV_MAX_CARACTERES y CNV_MAX_MB. Un valor invalido o no positivo cae al defecto."""
        e = os.environ if entorno is None else entorno
        base = cls()

        def leer(nombre, defecto):
            try:
                v = int(e.get(nombre, ""))
            except ValueError:
                return defecto
            return v if v > 0 else defecto

        return cls(leer("CNV_MAX_PAGINAS", base.paginas), leer("CNV_MAX_CARACTERES", base.caracteres), leer("CNV_MAX_MB", base.mb))


class ErrorDescarga(Exception):
    """Fallo de un adjunto. `reintentable` dice si vale la pena volver a probar."""

    def __init__(self, motivo: str, reintentable: bool = True):
        super().__init__(motivo)
        self.motivo = motivo
        self.reintentable = reintentable


def adjuntos(html: str) -> list:
    """[{"guid","nombre"}] de la pagina de una presentacion (JSON embebido en un tag <propiedad uploader=...>)."""
    salida = []
    for bloque in _RE_UPLOADER.findall(html):
        crudo = html_lib.unescape(bloque).strip()
        if not crudo.startswith("["):
            continue
        try:
            for a in json.loads(crudo):
                if isinstance(a, dict) and a.get("guid") and a.get("nombreArchivo"):
                    salida.append({"guid": a["guid"], "nombre": a["nombreArchivo"]})
        except json.JSONDecodeError:
            continue
    return salida


def _bajar_una_vez(guid: str, s, max_bytes: int) -> bytes | None:
    """Un intento. None si el adjunto esta vacio (no tiene sentido reintentar); ErrorDescarga si se corto."""
    try:
        resp = s.get(URL_VALET.format(guid=guid), timeout=90)
        if resp.status_code >= 500 or resp.status_code in (408, 429):
            raise ErrorDescarga(f"http {resp.status_code} al pedir la llave")
        llave = resp.json()["valetKeyData"]
        r = s.post(URL_BLOB.format(guid=guid), data={"ValetKey": llave}, timeout=240)
    except ErrorDescarga:
        raise
    except Exception as error:  # red cortada, timeout, JSON roto, llave ausente
        raise ErrorDescarga(f"{type(error).__name__}: {error}"[:120]) from error
    if r.status_code >= 500 or r.status_code in (408, 429):
        raise ErrorDescarga(f"http {r.status_code}")
    if r.status_code != 200:
        raise ErrorDescarga(f"http {r.status_code}", reintentable=False)
    contenido = r.content
    if len(contenido) > max_bytes:
        raise ErrorDescarga(f"archivo de {len(contenido) // 1_000_000} MB supera el tope", reintentable=False)
    return contenido if len(contenido) > 100 else None


def descargar_bytes(guid: str, s=None, max_bytes: int = 80_000_000, intentos: int = INTENTOS_ADJUNTO,
                    espera: float = ESPERA_BASE, hasta_t: float | None = None, dormir=time.sleep) -> bytes | None:
    """Bytes del adjunto, o None si esta vacio. Reintenta fallos transitorios con espera creciente hasta `hasta_t`
    (reloj monotonico); si se agotan los intentos lanza ErrorDescarga con el ultimo motivo."""
    s = s or sesion()
    ultimo = None
    for n in range(intentos):
        try:
            return _bajar_una_vez(guid, s, max_bytes)
        except ErrorDescarga as error:
            ultimo = error
            if not error.reintentable or n == intentos - 1:
                break
            pausa = espera * (n + 1)
            if hasta_t is not None and time.monotonic() + pausa > hasta_t:
                break  # no alcanza el tiempo del documento para otro intento
            dormir(pausa)
    raise ultimo


def pdfs_de(contenido: bytes) -> list:
    """[(nombre, bytes)] de PDFs: el propio contenido o los PDFs dentro de un ZIP."""
    if contenido[:4] == b"%PDF":
        return [("documento.pdf", contenido)]
    if contenido[:4] == _ZIP:
        try:
            with zipfile.ZipFile(io.BytesIO(contenido)) as z:
                return [(n, z.read(n)) for n in z.namelist() if n.lower().endswith(".pdf")]
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, OSError):
            return []  # ZIP corrupto, con cifrado o con compresion no soportada
    return []


def extraer_pdf(pdf: bytes, max_paginas: int = 400, max_caracteres: int = 1_500_000) -> dict:
    """{"texto","paginas","leidas","truncado","cifrado","ilegible"}. `paginas` es el total real del PDF; `leidas`, las que entraron
    en el texto; `truncado` es None, "paginas" o "caracteres". Texto vacio si el PDF es una imagen escaneada, esta cifrado o roto."""
    from pypdf import PdfReader

    vacio = {"texto": "", "paginas": 0, "leidas": 0, "truncado": None, "cifrado": False, "ilegible": False}
    try:
        lector = PdfReader(io.BytesIO(pdf))
        if lector.is_encrypted and not lector.decrypt(""):  # muchos PDF "protegidos" abren con clave vacia
            return {**vacio, "cifrado": True}
        total = len(lector.pages)
    except Exception:
        return {**vacio, "ilegible": True}
    paginas, usado, truncado = [], 0, None
    for n in range(min(total, max_paginas)):
        try:
            t = (lector.pages[n].extract_text() or "").strip()
        except Exception:
            t = ""
        bloque = f"[pagina {n + 1}]\n{t}"
        if usado + len(bloque) + 2 > max_caracteres:
            truncado = "caracteres"
            if not paginas:  # una sola pagina gigante: se recorta la pagina
                paginas.append(bloque[:max_caracteres])
            break
        paginas.append(bloque)
        usado += len(bloque) + 2
    else:
        if total > max_paginas:
            truncado = "paginas"
    return {**vacio, "texto": "\n\n".join(paginas), "paginas": total, "leidas": len(paginas), "truncado": truncado}


def texto_de_pdf(pdf: bytes, max_paginas: int = 400) -> tuple:
    """(texto con marcas de pagina, cantidad de paginas). Texto vacio si el PDF es una imagen escaneada."""
    r = extraer_pdf(pdf, max_paginas)
    return r["texto"], r["paginas"]


def texto_de_presentacion(url: str, s=None, limites: Limites | None = None, hasta_t: float | None = None, dormir=time.sleep) -> dict:
    """Datos del adjunto de una presentacion; {} si no hay adjunto o ninguno se pudo bajar.

    {"texto","paginas","archivos","escaneado","truncado","fallos"}: `truncado` es None o
    {"motivo","paginas_total","paginas_leidas"}; `fallos` lista los adjuntos que no se pudieron bajar o leer
    ([{"archivo","motivo"}]): si hay texto y fallos, el resultado es parcial y se vuelve a intentar mas tarde.
    Lanza la excepcion de red si falla el propio listado del adjunto (la pagina de la presentacion)."""
    s = s or sesion()
    limites = limites or Limites()
    tope = time.monotonic() + TIEMPO_MAX_DOCUMENTO
    hasta_t = min(hasta_t, tope) if hasta_t else tope
    r = s.get(url, timeout=120)
    r.raise_for_status()
    r.encoding = "utf-8"
    lista = adjuntos(r.text)
    if not lista:
        return {}
    partes, paginas, nombres, fallos = [], 0, [], []
    cupo_pag, cupo_car, truncado = limites.paginas, limites.caracteres, None
    for a in lista[:MAX_ADJUNTOS]:
        if time.monotonic() > hasta_t:
            fallos.append({"archivo": a["nombre"], "motivo": "sin tiempo"})
            continue
        try:
            contenido = descargar_bytes(a["guid"], s, limites.mb * 1_000_000, hasta_t=hasta_t, dormir=dormir)
        except ErrorDescarga as error:
            fallos.append({"archivo": a["nombre"], "motivo": error.motivo})
            continue
        if contenido is None:
            fallos.append({"archivo": a["nombre"], "motivo": "adjunto vacio"})
            continue
        pdfs = pdfs_de(contenido)
        if not pdfs:
            fallos.append({"archivo": a["nombre"], "motivo": "no es PDF ni ZIP con PDF"})
            continue
        for nombre, pdf in pdfs:
            etiqueta = a["nombre"] if nombre == "documento.pdf" else nombre
            if cupo_pag <= 0 or cupo_car <= 0:  # el cupo del documento ya se gasto en adjuntos anteriores
                truncado = truncado or {"motivo": "paginas" if cupo_pag <= 0 else "caracteres", "paginas_total": 0, "paginas_leidas": 0}
                continue
            e = extraer_pdf(pdf, cupo_pag, cupo_car)
            if e["cifrado"] or e["ilegible"]:
                fallos.append({"archivo": etiqueta, "motivo": "pdf cifrado" if e["cifrado"] else "pdf ilegible"})
                continue
            partes.append(e["texto"])
            nombres.append(etiqueta)
            paginas += e["paginas"]
            cupo_pag -= e["leidas"]
            cupo_car -= len(e["texto"]) + 2
            if e["truncado"]:
                truncado = truncado or {"motivo": e["truncado"], "paginas_total": e["paginas"], "paginas_leidas": e["leidas"]}
    texto = "\n\n".join(p for p in partes if p)
    if not texto.strip() and not partes:
        return {"fallos": fallos} if fallos else {}
    util = len(re.sub(r"\[pagina \d+\]|\s", "", texto))
    return {"texto": texto, "paginas": paginas, "archivos": nombres, "truncado": truncado, "fallos": fallos,
            "escaneado": bool(paginas) and util < 40 * max(paginas, 1) * 0.1}
