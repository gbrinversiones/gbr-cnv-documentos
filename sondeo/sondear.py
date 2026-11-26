"""Sondeo temporal: BOPREAL Serie 4 en el BCRA y AO28 en el Boletin (no se mergea)."""
import io, re, time, requests
from pypdf import PdfReader
s = requests.Session(); s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; gbr-biblioteca/1.0)"})
def texto_pdf(u, paginas=3):
    r = s.get(u, timeout=40)
    if not (r.ok and r.content[:4] == b"%PDF"):
        return None
    return re.sub(r"\s+", " ", " ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages[:paginas]))
for aviso in ["341303/20260429", "343851/20260701", "342001/20260515", "342495/20260528"]:
    t = texto_pdf(f"https://www.boletinoficial.gob.ar/pdf/aviso/primera/{aviso}", 6) or ""
    for m in re.finditer(r"octubre de 2028", t):
        print("BO", aviso, "|", t[max(0, m.start()-350):m.end()+150])
    time.sleep(3)
for n in range(8190, 8330):
    try:
        t = texto_pdf(f"https://www.bcra.gob.ar/Pdfs/comytexord/A{n}.pdf", 2)
    except Exception as e:
        print(f"A{n} error {e}"); continue
    if t and "BOPREAL" in t.upper():
        fecha = re.search(r"\d{2}/\d{2}/\d{4}", t)
        k = t.upper().find("SERIE")
        print(f"A{n}", fecha and fecha.group(0), "|", t[:120], "|", t[max(0, k-150):k+250] if k >= 0 else "")
    time.sleep(0.7)
