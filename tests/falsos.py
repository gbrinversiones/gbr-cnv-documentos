"""Dobles reutilizables para las pruebas de punta a punta: una CNV falsa (listado, paginas, descargas) y un Anthropic falso.
Nada sale a la red y no hay claves: el unico dato 'secreto' es la variable ANTHROPIC_API_KEY=de-prueba que se pone en el entorno."""
import json
from types import SimpleNamespace

from cnv import avisos_pago, clasificacion, financieros, resumenes


class CnvFalsa:
    """Sesion requests falsa: sirve el listado de una empresa, la pagina de cada presentacion y los PDF."""

    def __init__(self, listado_html: str, pdfs: dict, defecto: bytes):
        self.listado_html, self.pdfs, self.defecto = listado_html, pdfs, defecto
        self.headers, self.verify = {}, False
        self.bajadas = []  # guid de cada descarga de adjunto
        self.fallar = {}   # guid -> cantidad de POST que fallan antes de andar

    def get(self, url, params=None, timeout=None):
        if "GetPublicValetKey" in url:
            guid = url.split("GetPublicValetKey/")[1].split("?")[0]
            return SimpleNamespace(status_code=200, json=lambda: {"valetKeyData": "k-" + guid})
        if "/Empresas/Empresa/" in url:
            html = self.listado_html
        else:  # publicview/<clave>
            clave = url.rsplit("/", 1)[1]
            lista = json.dumps([{"guid": clave, "nombreArchivo": f"{clave}.pdf"}]).replace('"', "&quot;")
            html = f'<html><propiedad uploader="u">{lista}</propiedad></html>'
        return SimpleNamespace(status_code=200, text=html, encoding=None, raise_for_status=lambda: None)

    def post(self, url, data=None, timeout=None):
        guid = url.rsplit("/", 1)[1]
        self.bajadas.append(guid)
        if self.fallar.get(guid, 0) > 0:
            self.fallar[guid] -= 1
            return SimpleNamespace(status_code=503, content=b"")
        return SimpleNamespace(status_code=200, content=self.pdfs.get(guid, self.defecto))


class AnthropicFalso:
    """Doble de anthropic.Anthropic(): contesta segun el prompt de sistema de cada etapa y cuenta las llamadas."""

    def __init__(self, respuestas: dict):
        self.respuestas = respuestas  # nombre de etapa -> dict/str/Exception
        self.llamadas = {}
        self.messages = SimpleNamespace(create=self._crear)

    @staticmethod
    def etapa(sistema: str) -> str:
        for nombre, mod in (("financieros", financieros), ("avisos_pago", avisos_pago), ("clasificacion", clasificacion)):
            if sistema == mod.INSTRUCCIONES:
                return nombre
        return "resumenes" if sistema == resumenes.PROMPT else "?"

    def _crear(self, **kw):
        etapa = self.etapa(kw["system"])
        self.llamadas[etapa] = self.llamadas.get(etapa, 0) + 1
        x = self.respuestas[etapa]
        if isinstance(x, Exception):
            raise x
        texto = x if isinstance(x, str) else json.dumps(x)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=texto)])

    def total(self) -> int:
        return sum(self.llamadas.values())
