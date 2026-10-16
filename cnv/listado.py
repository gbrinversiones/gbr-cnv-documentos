"""Listado de documentos de una empresa en la CNV (pagina publica de Empresas)."""
import datetime as dt
import re

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
URL_LISTADO = "https://www.cnv.gov.ar/SitioWeb/Empresas/Empresa/{cuit}?fdesde={desde}"
_MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12}

# Secciones de la pagina -> tipo normalizado (el orden importa: gana el primer prefijo que coincide)
TIPOS = (
    ("hechos", ("hechos relevantes",)),
    ("estados_contables", ("estados contables", "informacion financiera", "información financiera")),
    ("prospectos", ("prospectos", "suplementos", "avisos de resultado", "aviso de resultado", "aviso de suscrip")),
    ("avisos_pago", ("aviso de pago", "avisos de pago")),
    ("actas", ("acta",)),
)


def sesion() -> requests.Session:
    s = requests.Session()
    s.verify = False
    s.headers.update({"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9"})
    return s


def parsear_fecha(txt: str) -> str:
    """"10 ago. 2026 15:27" -> "2026-08-10"; "" si no se entiende (nunca inventa una fecha)."""
    t = txt.strip().lower().replace(".", "")
    m = re.match(r"(\d{1,2})\s+([a-záéíóú]+)\s+(\d{4})", t)
    mes = _MESES.get(m.group(2)[:3]) if m else None
    return f"{m.group(3)}-{mes:02d}-{int(m.group(1)):02d}" if m and mes else ""


def tipo_de_seccion(seccion: str) -> str:
    s = seccion.lower().strip()
    for tipo, prefijos in TIPOS:
        if s.startswith(prefijos):
            return tipo
    return "otros"


def parsear_listado(html: str) -> list:
    """[{"doc_id","fecha","seccion","tipo","descripcion","url"}] de todas las secciones, mas nuevos primero."""
    soup = BeautifulSoup(html, "html.parser")
    por_id = {}
    for tabla in soup.find_all("table", class_="tabla-hechos-relevantes"):
        h = tabla.find_previous(re.compile(r"^h[1-5]$"))
        seccion = h.get_text(strip=True) if h else ""
        for tr in tabla.find_all("tr"):
            td = tr.find_all("td")
            if len(td) < 5:
                continue
            a = td[4].find("a", href=True)
            doc_id = td[3].get_text(strip=True)
            if not a or "publicview" not in a["href"] or not doc_id.isdigit():
                continue
            fila = {
                "doc_id": doc_id, "fecha": parsear_fecha(td[0].get_text()), "seccion": seccion, "tipo": tipo_de_seccion(seccion),
                "descripcion": " ".join(td[2].get_text(strip=True).split()), "url": a["href"].strip(),
            }
            # "Ultima Informacion Recibida" repite documentos de otras secciones: gana la seccion que los tipifica
            previa = por_id.get(doc_id)
            if previa is None or (previa["tipo"] == "otros" and fila["tipo"] != "otros"):
                por_id[doc_id] = fila
    return sorted(por_id.values(), key=lambda f: int(f["doc_id"]), reverse=True)


def listar(cuit: str, desde: dt.date, s: requests.Session | None = None) -> list:
    """Documentos de la empresa publicados desde `desde`. Lanza si la CNV no responde (el llamador decide reintentar)."""
    s = s or sesion()
    url = URL_LISTADO.format(cuit=cuit.replace("-", ""), desde=desde.strftime("%d/%m/%Y"))
    r = s.get(url, timeout=120)
    r.raise_for_status()
    r.encoding = "utf-8"
    return parsear_listado(r.text)
