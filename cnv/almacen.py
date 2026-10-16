"""Almacenamiento: Google Cloud Storage en produccion, carpeta local en pruebas.

Estructura del bucket:
  cnv/texto/{cuit}/{doc_id}.txt.gz      texto completo del documento (con marcas [pagina N])
  cnv/indice/{cuit}.json                metadatos de todos los documentos de la empresa
  cnv/indice/global.json.gz             metadatos de todas las empresas (lo que busca el explorador)
"""
import gzip
import json
import os


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
        b = self.bucket.blob(nombre)
        return b.download_as_bytes() if b.exists() else None

    def existe(self, nombre: str) -> bool:
        return self.bucket.blob(nombre).exists()


def almacen_desde_entorno():
    """GCS si hay GCS_BUCKET (y GCP_SA_KEY), si no una carpeta local ./datos."""
    bucket = os.environ.get("GCS_BUCKET")
    if bucket:
        return AlmacenGCS(bucket, os.environ.get("GCP_SA_KEY"))
    return AlmacenLocal(os.environ.get("DATOS_LOCAL", "datos"))


def guardar_texto(almacen, cuit: str, doc_id: str, texto: str) -> None:
    almacen.guardar(f"cnv/texto/{cuit}/{doc_id}.txt.gz", gzip.compress(texto.encode("utf-8")))


def leer_texto(almacen, cuit: str, doc_id: str) -> str | None:
    crudo = almacen.leer(f"cnv/texto/{cuit}/{doc_id}.txt.gz")
    return gzip.decompress(crudo).decode("utf-8") if crudo else None


def guardar_indice_empresa(almacen, cuit: str, docs: list) -> None:
    almacen.guardar(f"cnv/indice/{cuit}.json", json.dumps(docs, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def leer_indice_empresa(almacen, cuit: str) -> list:
    crudo = almacen.leer(f"cnv/indice/{cuit}.json")
    return json.loads(crudo) if crudo else []
