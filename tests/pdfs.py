"""Generador de PDFs sinteticos para las pruebas (sin dependencias extra): paginas con texto, y variantes cifradas/rotas."""
import io


def _escapar(t: str) -> str:
    return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf_con_paginas(paginas: list) -> bytes:
    """PDF valido con un texto por pagina (Helvetica, una linea por renglon). Lista vacia -> PDF sin paginas."""
    objs = []  # cuerpo de cada objeto, 1-indexado: 1 catalogo, 2 paginas, 3 fuente, luego (pagina, contenido) por pagina
    n = len(paginas)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs.append("<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, texto in enumerate(paginas):
        lineas = texto.split("\n")
        ops = ["BT", "/F1 10 Tf", "12 TL", "40 750 Td"] + [f"({_escapar(l)}) Tj T*" for l in lineas] + ["ET"]
        flujo = "\n".join(ops)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {5 + 2 * i} 0 R /Resources << /Font << /F1 3 0 R >> >> >>")
        objs.append(f"<< /Length {len(flujo)} >>\nstream\n{flujo}\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for k, cuerpo in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{k} 0 obj\n{cuerpo}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offsets:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def pdf_cifrado(paginas: list, clave: str = "secreta") -> bytes:
    """El mismo PDF cifrado con clave de usuario (no abre con clave vacia)."""
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_con_paginas(paginas))))
    w.encrypt(clave, algorithm="AES-128")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def pdf_cifrado_clave_vacia(paginas: list) -> bytes:
    """Cifrado con clave de usuario vacia (solo restringe permisos): se puede leer."""
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_con_paginas(paginas))))
    w.encrypt("", owner_password="duenio", algorithm="AES-128")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()
