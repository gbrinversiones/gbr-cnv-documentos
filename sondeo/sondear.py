"""Sondeo temporal: comunicaciones del BCRA sobre BOPREAL (no se mergea)."""
import io, re, requests
s = requests.Session(); s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; gbr-biblioteca/1.0)"})
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None
for n in [7918, 7925, 7935, 7937, 7980, 7999, 8019, 8020, 8022, 8032, 8038, 8048, 8053, 8062, 8085, 8100, 8123, 8155]:
    u = f"https://www.bcra.gob.ar/Pdfs/comytexord/A{n}.pdf"
    try:
        r = s.get(u, timeout=40)
        t = ""
        if r.ok and r.content[:4] == b"%PDF" and PdfReader:
            t = " ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages[:2])
        t = re.sub(r"\s+", " ", t)
        k = t.upper().find("BOPREAL")
        print(f"A{n}", r.status_code, r.headers.get("content-type"), len(r.content), "BOPREAL" if k >= 0 else "-", "|", t[:160], "|", t[max(0, k-100):k+300] if k >= 0 else "")
    except Exception as e:
        print(f"A{n} error {e}")
