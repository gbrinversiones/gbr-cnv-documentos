"""Descarga del adjunto principal de una presentacion de la CNV y extraccion de su texto."""
import html as html_lib
import io
import json
import re
import zipfile

from .listado import sesion

URL_VALET = "https://aif2.cnv.gov.ar/api/ValetKeyProvider/GetPublicValetKey/{guid}?operation=DownloadBlob"
URL_BLOB = "https://blob.cnv.gov.ar/BlobWebService.svc/DownloadBlob/{guid}"
_RE_UPLOADER = re.compile(r'<propiedad[^>]*uploader="[^"]*"[^>]*>(.*?)</propiedad>', re.S | re.I)
_ZIP = b"PK" + bytes([3, 4])


def adjuntos(html: str) -> list:
    """[{"guid","nombre"}] de la pagina de una presentacion (JSON embebido en un tag <propiedad uploader=...>)."""
    salida = []
    for bloque in _RE_UPLOADER.findall(html):
        crudo = html_lib.unescape(bloque).strip()
        if not crudo.startswith("["):
            continue
        try:
            for a in json.loads(crudo):
                if a.get("guid") and a.get("nombreArchivo"):
                    salida.append({"guid": a["guid"], "nombre": a["nombreArchivo"]})
        except json.JSONDecodeError:
            continue
    return salida


def descargar_bytes(guid: str, s=None) -> bytes | None:
    s = s or sesion()
    k = s.get(URL_VALET.format(guid=guid), timeout=90).json()
    r = s.post(URL_BLOB.format(guid=guid), data={"ValetKey": k["valetKeyData"]}, timeout=240)
    return r.content if r.status_code == 200 and len(r.content) > 100 else None


def pdfs_de(contenido: bytes) -> list:
    """[(nombre, bytes)] de PDFs: el propio contenido o los PDFs dentro de un ZIP."""
    if contenido[:4] == b"%PDF":
        return [("documento.pdf", contenido)]
    if contenido[:4] == _ZIP:
        try:
            with zipfile.ZipFile(io.BytesIO(contenido)) as z:
                return [(n, z.read(n)) for n in z.namelist() if n.lower().endswith(".pdf")]
        except zipfile.BadZipFile:
            return []
    return []


def texto_de_pdf(pdf: bytes, max_paginas: int = 400) -> tuple:
    """(texto con marcas de pagina, cantidad de paginas). Texto vacio si el PDF es una imagen escaneada."""
    from pypdf import PdfReader

    lector = PdfReader(io.BytesIO(pdf))
    paginas = []
    for n, p in enumerate(lector.pages[:max_paginas], 1):
        try:
            t = (p.extract_text() or "").strip()
        except Exception:
            t = ""
        paginas.append(f"[pagina {n}]\n{t}")
    return "\n\n".join(paginas), len(lector.pages)


def texto_de_presentacion(url: str, s=None) -> dict:
    """{"texto","paginas","archivos","escaneado"} del adjunto de una presentacion; {} si no hay adjunto o falla la descarga."""
    s = s or sesion()
    r = s.get(url, timeout=120)
    r.raise_for_status()
    r.encoding = "utf-8"
    lista = adjuntos(r.text)
    if not lista:
        return {}
    partes, paginas, nombres = [], 0, []
    for a in lista[:3]:  # algunas presentaciones traen varios adjuntos
        contenido = descargar_bytes(a["guid"], s)
        for nombre, pdf in pdfs_de(contenido or b""):
            t, n = texto_de_pdf(pdf)
            partes.append(t)
            paginas += n
            nombres.append(a["nombre"] if nombre == "documento.pdf" else nombre)
    texto = "\n\n".join(partes)
    util = len(re.sub(r"\[pagina \d+\]|\s", "", texto))
    return {"texto": texto, "paginas": paginas, "archivos": nombres, "escaneado": bool(paginas) and util < 40 * max(paginas, 1) * 0.1}
