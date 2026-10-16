"""OCR OPCIONAL de PDF escaneados (pytesseract + pdf2image). Apagado salvo que CNV_OCR=1.

Por que opcional: necesita el binario `tesseract` (con el idioma espanol) y `poppler`, que no estan en el job normal,
y el OCR puede leer mal un digito. Por eso los documentos con OCR se marcan (`ocr` = paginas reconocidas) y las etapas que
publican cifras (financieros, avisos_pago) NO los usan: una cita literal sobre texto mal reconocido seguiria "verificando".

Variables: CNV_OCR (1/true/si), CNV_OCR_MAX_PAGINAS (40), CNV_OCR_IDIOMA (spa), CNV_OCR_DPI (200), CNV_OCR_CONFIANZA (70).
"""
import os
import re

MIN_CARACTERES_POR_PAGINA = 25  # menos que esto en promedio: el PDF es (casi) una imagen y vale la pena probar OCR


class OcrNoDisponible(Exception):
    """Falta una libreria, el binario tesseract o el idioma: no es culpa del documento, no se marca como intentado."""


def _entero(valor, defecto: int) -> int:
    try:
        v = int(valor)
    except (TypeError, ValueError):
        return defecto
    return v if v > 0 else defecto


def activado(entorno=None) -> bool:
    e = os.environ if entorno is None else entorno
    return str(e.get("CNV_OCR", "")).strip().lower() in ("1", "true", "si", "yes")


def configuracion(entorno=None) -> dict:
    e = os.environ if entorno is None else entorno
    return {"max_paginas": _entero(e.get("CNV_OCR_MAX_PAGINAS"), 40), "idioma": (e.get("CNV_OCR_IDIOMA") or "spa").strip(), "dpi": _entero(e.get("CNV_OCR_DPI"), 200)}


def necesita_ocr(texto: str, paginas: int) -> bool:
    """True si el texto extraido es casi vacio para la cantidad de paginas (se descuentan las marcas [pagina N])."""
    if paginas <= 0:
        return False
    util = len(re.sub(r"\[pagina \d+\]|\s", "", texto))
    return util / paginas < MIN_CARACTERES_POR_PAGINA


def confianza_minima(entorno=None) -> int:
    e = os.environ if entorno is None else entorno
    v = _entero(e.get("CNV_OCR_CONFIANZA"), 70)
    return min(v, 100)


ILEGIBLE = "[ilegible]"


def texto_con_confianza(datos: dict, minima: int) -> tuple:
    """(texto por lineas, palabras_numericas, palabras_numericas_ilegibles) a partir del dict de pytesseract.image_to_data.
    Toda palabra que contiene un digito y se leyo con menos de `minima` de confianza se reemplaza por [ilegible]:
    un numero mal leido es peor que uno ausente. Las palabras sin digitos se dejan (un rotulo mal leido no inventa datos)."""
    lineas, clave_actual, actual = [], None, []
    numericas = ilegibles = 0
    for i, palabra in enumerate(datos.get("text", [])):
        palabra = str(palabra).strip()
        if not palabra:
            continue
        try:
            conf = float(datos["conf"][i])
        except (KeyError, IndexError, ValueError, TypeError):
            conf = -1.0
        if any(c.isdigit() for c in palabra):
            numericas += 1
            if conf < minima:
                palabra, ilegibles = ILEGIBLE, ilegibles + 1
        clave = tuple(datos.get(k, [0])[i] if i < len(datos.get(k, [])) else 0 for k in ("block_num", "par_num", "line_num"))
        if clave != clave_actual and actual:
            lineas.append(" ".join(actual))
            actual = []
        clave_actual = clave
        actual.append(palabra)
    if actual:
        lineas.append(" ".join(actual))
    return "\n".join(lineas), numericas, ilegibles


def ocr_pdf_confiable(pdf: bytes, paginas_totales: int, max_paginas: int = 40, idioma: str = "spa", dpi: int = 300, minima: int = 70) -> dict:
    """Como ocr_pdf, pero descarta (marca [ilegible]) los numeros de baja confianza. Para formularios donde los valores son el dato.
    {"texto","paginas_ocr","paginas_total","numericas","ilegibles"}. Lanza OcrNoDisponible si falta algo del entorno."""
    comprobar(idioma)
    import pytesseract
    from pdf2image import convert_from_bytes

    partes, hechas, num, ileg = [], 0, 0, 0
    for n in range(1, min(paginas_totales, max_paginas) + 1):
        try:
            imagenes = convert_from_bytes(pdf, dpi=dpi, first_page=n, last_page=n)
            datos = pytesseract.image_to_data(imagenes[0], lang=idioma, output_type=pytesseract.Output.DICT) if imagenes else {"text": []}
            t, a, b = texto_con_confianza(datos, minima)
        except Exception:  # una pagina rota no tira el documento
            t, a, b = "", 0, 0
        partes.append(f"[pagina {n}]\n{t}")
        hechas, num, ileg = hechas + 1, num + a, ileg + b
    return {"texto": "\n\n".join(partes), "paginas_ocr": hechas, "paginas_total": paginas_totales, "numericas": num, "ilegibles": ileg}


def comprobar(idioma: str = "spa") -> None:
    """Lanza OcrNoDisponible si falta pytesseract, pdf2image, el binario tesseract o el idioma pedido."""
    try:
        import pdf2image  # noqa: F401
        import pytesseract
    except ImportError as error:
        raise OcrNoDisponible(f"falta {error.name}") from error
    try:
        disponibles = set(pytesseract.get_languages(config=""))
    except Exception as error:  # TesseractNotFoundError y similares
        raise OcrNoDisponible(f"tesseract no disponible: {type(error).__name__}") from error
    faltan = [i for i in idioma.split("+") if i not in disponibles]
    if faltan:
        raise OcrNoDisponible(f"falta el idioma de tesseract: {','.join(faltan)}")


def ocr_pdf(pdf: bytes, paginas_totales: int, max_paginas: int = 40, idioma: str = "spa", dpi: int = 200) -> dict:
    """{"texto" (con marcas [pagina N]), "paginas_ocr", "paginas_total"}. Una pagina a la vez para acotar la memoria.
    Lanza OcrNoDisponible si falta algo del entorno."""
    comprobar(idioma)
    import pytesseract
    from pdf2image import convert_from_bytes

    partes, hechas = [], 0
    for n in range(1, min(paginas_totales, max_paginas) + 1):
        try:
            imagenes = convert_from_bytes(pdf, dpi=dpi, first_page=n, last_page=n)
            t = pytesseract.image_to_string(imagenes[0], lang=idioma).strip() if imagenes else ""
        except Exception:  # una pagina rota no tira el documento
            t = ""
        partes.append(f"[pagina {n}]\n{t}")
        hechas += 1
    return {"texto": "\n\n".join(partes), "paginas_ocr": hechas, "paginas_total": paginas_totales}
