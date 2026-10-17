"""Almacenamiento: Google Cloud Storage en produccion, carpeta local en pruebas.

Estructura del bucket:
  cnv/texto/{cuit}/{doc_id}.txt.gz      texto completo del documento (con marcas [pagina N])
  cnv/texto/{cuit}/{doc_id}.ocr.txt.gz  texto leido por OCR de un formulario de aviso de pago (solo ese caso; ver cnv/avisos_pago.py)
  cnv/indice/{cuit}.json                metadatos de todos los documentos de la empresa
  cnv/indice/global.json.gz             metadatos de todas las empresas (lo que busca el explorador)
  cnv/financieros/{cuit}.json           cifras clave por periodo, con cita textual (ver cnv/financieros.py)
  cnv/indice/financieros.json.gz        tabla plana empresa/periodo/cifras
"""
import gzip
import hashlib
import io
import json
import os


def comprimir(datos: bytes) -> bytes:
    """gzip DETERMINISTA (mtime=0): el mismo contenido da siempre los mismos bytes. gzip.compress() escribe la hora actual en la cabecera,
    asi que dos corridas identicas daban archivos distintos (se resubian sin necesidad y las pruebas de idempotencia fallaban a veces)."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as f:
        f.write(datos)
    return buf.getvalue()


def guardar_si_cambio(almacen, nombre: str, datos: bytes) -> bool:
    """Escribe solo si el contenido es distinto del que ya esta (no resube lo que no cambio). True si escribio."""
    if almacen.leer(nombre) == datos:
        return False
    almacen.guardar(nombre, datos)
    return True


class AlmacenLocal:
    def __init__(self, raiz: str):
        self.raiz = raiz

    def _ruta(self, nombre: str) -> str:
        p = os.path.join(self.raiz, *nombre.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def guardar(self, nombre: str, datos: bytes) -> None:
        with open(self._ruta(nombre), "wb") as f:
            f.write(datos)

    def leer(self, nombre: str) -> bytes | None:
        p = self._ruta(nombre)
        return open(p, "rb").read() if os.path.exists(p) else None

    def existe(self, nombre: str) -> bool:
        return os.path.exists(self._ruta(nombre))

    def firmas(self, prefijo: str) -> dict:
        """{nombre: huella del contenido} de todo lo que empieza con `prefijo`. Sirve para saber si algo cambio SIN descargarlo (en GCS es un solo listado)."""
        salida = {}
        base = os.path.join(self.raiz, *prefijo.split("/")[:-1]) if "/" in prefijo else self.raiz
        for carpeta, _, archivos in os.walk(base):
            for a in archivos:
                nombre = os.path.relpath(os.path.join(carpeta, a), self.raiz).replace(os.sep, "/")
                if nombre.startswith(prefijo):
                    with open(os.path.join(carpeta, a), "rb") as f:
                        salida[nombre] = hashlib.sha1(f.read()).hexdigest()
        return salida


def _es_no_encontrado(error: Exception) -> bool:
    """True si `error` es google.api_core.exceptions.NotFound. Se usa la clase REAL; solo si el paquete no se puede importar se reconoce por el nombre de la clase."""
    try:
        from google.api_core.exceptions import NotFound
    except ImportError:
        return type(error).__name__ == "NotFound"
    return isinstance(error, NotFound)


class AlmacenGCS:
    def __init__(self, bucket: str, credenciales_json: str | None = None):
        from google.cloud import storage

        if credenciales_json:
            cliente = storage.Client.from_service_account_info(json.loads(credenciales_json))
        else:
            cliente = storage.Client()
        self.bucket = cliente.bucket(bucket)

    def guardar(self, nombre: str, datos: bytes) -> None:
        self.bucket.blob(nombre).upload_from_string(datos)

    def leer(self, nombre: str) -> bytes | None:
        """UN pedido (antes eran dos: `exists()` y despues la descarga). Un objeto inexistente es None; cualquier otro error se propaga."""
        try:
            return self.bucket.blob(nombre).download_as_bytes()
        except Exception as error:
            if not _es_no_encontrado(error):
                raise
            return None

    def existe(self, nombre: str) -> bool:
        return self.bucket.blob(nombre).exists()

    def firmas(self, prefijo: str) -> dict:
        """{nombre: huella} de lo que empieza con `prefijo`, en UN listado (md5 / crc32c / generacion que informa GCS; no descarga nada)."""
        return {b.name: b.md5_hash or b.crc32c or str(b.generation) for b in self.bucket.list_blobs(prefix=prefijo)}


def almacen_desde_entorno():
    """GCS si hay GCS_BUCKET (y GCP_SA_KEY), si no una carpeta local ./datos."""
    bucket = os.environ.get("GCS_BUCKET")
    if bucket:
        return AlmacenGCS(bucket, os.environ.get("GCP_SA_KEY"))
    return AlmacenLocal(os.environ.get("DATOS_LOCAL", "datos"))


def guardar_texto(almacen, cuit: str, doc_id: str, texto: str) -> None:
    almacen.guardar(f"cnv/texto/{cuit}/{doc_id}.txt.gz", comprimir(texto.encode("utf-8")))


def leer_texto(almacen, cuit: str, doc_id: str) -> str | None:
    crudo = almacen.leer(f"cnv/texto/{cuit}/{doc_id}.txt.gz")
    return gzip.decompress(crudo).decode("utf-8") if crudo else None


def guardar_indice_empresa(almacen, cuit: str, docs: list) -> None:
    almacen.guardar(f"cnv/indice/{cuit}.json", json.dumps(docs, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def leer_indice_empresa(almacen, cuit: str) -> list:
    crudo = almacen.leer(f"cnv/indice/{cuit}.json")
    return json.loads(crudo) if crudo else []


def guardar_texto_ocr(almacen, cuit: str, doc_id: str, texto: str) -> None:
    almacen.guardar(f"cnv/texto/{cuit}/{doc_id}.ocr.txt.gz", comprimir(texto.encode("utf-8")))


def leer_texto_ocr(almacen, cuit: str, doc_id: str) -> str | None:
    crudo = almacen.leer(f"cnv/texto/{cuit}/{doc_id}.ocr.txt.gz")
    return gzip.decompress(crudo).decode("utf-8") if crudo else None
