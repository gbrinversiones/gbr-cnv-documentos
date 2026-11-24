"""`python -m cnv vision`: lectura con Claude (Haiku, PDF como imagen) de los documentos que quedaron SIN TEXTO aun despues del OCR.

Tesseract (cnv/ocr.py) es gratis pero falla con escaneos torcidos, sellos, tablas densas o baja resolucion. Para esos casos el PDF se manda
al modelo como documento y se pide una transcripcion literal pagina por pagina. Costo de lista de Haiku: unos US$ 0,0002 por pagina de
entrada mas la transcripcion de salida (~US$ 0,0005 por pagina): un documento de 30 paginas sale menos de 3 centavos. El tope de dolares
del comando (CNV_MAX_USD) corta limpio, como en los demas pasos con modelo.

El texto queda marcado `ocr` (paginas leidas) y `vision` en el indice: las etapas que publican cifras lo tratan como texto reconocido (menos
confiable que el texto del PDF), igual que el OCR. Un documento se intenta UNA vez (`vision_intentado`).
"""
import base64
import io
import re
import time

from . import almacen as alm
from .concurrencia import crear_mensaje
from .modelo import MODELO

MAX_PAGINAS = 40           # por documento (las primeras: caratula, estados principales, notas iniciales)
PAGINAS_POR_LLAMADA = 10   # la transcripcion de 10 paginas entra holgada en la salida
MAX_TOKENS_SALIDA = 16_000
MIN_CARACTERES = 200
GUARDAR_CADA = 5

SISTEMA = (
    "Transcribis documentos escaneados presentados ante la Comision Nacional de Valores (Argentina). Copia el texto TAL CUAL aparece, "
    "en su idioma, sin resumir, traducir ni corregir. Respeta los numeros exactamente (puntos, comas, signos, parentesis). Las tablas van "
    "renglon por renglon con las columnas separadas por ' | '. Antes de cada pagina escribi una linea '[pagina N]' con el numero de pagina "
    "del archivo que te indican. Si una palabra o cifra no se lee, escribi [ilegible]; nunca la adivines. No agregues comentarios."
)


def candidatos(indice: list, tipos: tuple) -> list:
    """Documentos sin texto que ya pasaron por el OCR (o que agotaron sus intentos como escaneados) y nunca se mandaron al modelo.
    Los mas nuevos primero."""
    salida = [
        m for m in indice
        if m.get("t") in tipos and not m.get("texto_ok") and m.get("u") and not m.get("vision_intentado")
        and (m.get("ocr_intentado") or m.get("escaneado") or "sin texto" in (m.get("error") or ""))
    ]
    return sorted(salida, key=lambda m: (m.get("f") or "", int(m["id"])), reverse=True)


def partir_pdf(pdf: bytes, max_paginas: int = MAX_PAGINAS, por_parte: int = PAGINAS_POR_LLAMADA) -> list:
    """[(primera_pagina, bytes de un PDF con hasta `por_parte` paginas)] con las primeras `max_paginas` paginas."""
    from pypdf import PdfReader, PdfWriter

    lector = PdfReader(io.BytesIO(pdf))
    total = min(len(lector.pages), max_paginas)
    partes = []
    for inicio in range(0, total, por_parte):
        escritor = PdfWriter()
        for n in range(inicio, min(inicio + por_parte, total)):
            escritor.add_page(lector.pages[n])
        salida = io.BytesIO()
        escritor.write(salida)
        partes.append((inicio + 1, salida.getvalue()))
    return partes


def transcribir(cliente, pdf: bytes, primera: int) -> str:
    r = crear_mensaje(
        cliente,
        model=MODELO,
        max_tokens=MAX_TOKENS_SALIDA,
        thinking={"type": "disabled"},
        system=SISTEMA,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf).decode("ascii")}},
            {"type": "text", "text": f"Transcribi este fragmento. Su primera pagina es la pagina {primera} del archivo."},
        ]}],
    )
    return "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", None) == "text").strip()


def util(texto: str) -> int:
    """Caracteres con contenido (sin marcas de pagina, [ilegible] ni espacios)."""
    return len(re.sub(r"\[pagina \d+\]|\[ilegible\]|\s", "", texto))


def leer_documento(cliente, pdfs: list, max_paginas: int = MAX_PAGINAS) -> tuple:
    """(texto, paginas leidas) de los PDF de una presentacion, hasta `max_paginas` en total."""
    partes, paginas = [], 0
    for _, pdf in pdfs:
        if paginas >= max_paginas:
            break
        for primera, trozo in partir_pdf(pdf, max_paginas - paginas):
            partes.append(transcribir(cliente, trozo, primera))
        paginas += min(max_paginas - paginas, _paginas(pdf))
    return "\n\n".join(p for p in partes if p), paginas


def _paginas(pdf: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(pdf)).pages)


def ejecutar(almacen, empresas: list, cliente, tipos: tuple, max_docs: int, hasta_t: float, bajar_pdfs, prioridad: set | None = None) -> dict:
    """Recorre las empresas (primero las de `prioridad`) y lee con el modelo hasta `max_docs` documentos. `bajar_pdfs(url)` devuelve
    [(nombre, bytes)]. Devuelve {"leidos","sin_texto","fallidos","pendientes"}."""
    from .concurrencia import ErrorDeCuenta

    orden = sorted(empresas, key=lambda e: e["cuit"] not in (prioridad or set()))
    total = {"leidos": 0, "sin_texto": 0, "fallidos": 0, "pendientes": 0}
    enviados = 0
    for e in orden:
        indice = alm.leer_indice_empresa(almacen, e["cuit"])
        cola = candidatos(indice, tipos)
        if not cola:
            continue
        por_id = {m["id"]: m for m in indice}
        hechos = 0

        def guardar(cuit=e["cuit"], por_id=por_id):
            alm.guardar_indice_empresa(almacen, cuit, sorted(por_id.values(), key=lambda x: int(x["id"]), reverse=True))

        try:
            for m in cola:
                if enviados >= max_docs or time.monotonic() > hasta_t:
                    total["pendientes"] += 1
                    continue
                try:
                    pdfs = bajar_pdfs(m["u"])
                except Exception:  # noqa: BLE001  la CNV no respondio: no se marca, se reintenta en otra corrida
                    total["fallidos"] += 1
                    continue
                if not pdfs:
                    por_id[m["id"]] = {**m, "vision_intentado": True}
                    total["sin_texto"] += 1
                    continue
                enviados += 1
                try:
                    texto, paginas = leer_documento(cliente, pdfs)
                except ErrorDeCuenta:
                    raise
                except Exception as error:  # noqa: BLE001  un documento roto no frena la corrida
                    por_id[m["id"]] = {**m, "vision_intentado": True, "error": f"vision: {str(error)[:100]}"}
                    total["fallidos"] += 1
                    continue
                if util(texto) >= MIN_CARACTERES:
                    alm.guardar_texto(almacen, e["cuit"], m["id"], texto)
                    nuevo = {k: v for k, v in m.items() if k not in ("error", "parcial")}
                    nuevo.update({"texto_ok": True, "p": paginas, "n": len(texto), "ocr": paginas, "vision": True, "vision_intentado": True})
                    por_id[m["id"]] = nuevo
                    total["leidos"] += 1
                    print(f"  {e['empresa']} #{m['id']} ({m['t']}): {paginas} paginas leidas con el modelo", flush=True)
                else:
                    por_id[m["id"]] = {**m, "vision_intentado": True}
                    total["sin_texto"] += 1
                hechos += 1
                if hechos % GUARDAR_CADA == 0:
                    guardar()
        finally:
            guardar()
    return total
