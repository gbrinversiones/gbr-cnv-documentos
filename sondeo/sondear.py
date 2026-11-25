"""Sondeo temporal: URL del PDF de un aviso del Boletin Oficial (no se mergea)."""
import re, requests
s = requests.Session(); s.headers.update({"User-Agent": "Mozilla/5.0 (compatible; gbr-biblioteca/1.0)"})
B = "https://www.boletinoficial.gob.ar"
d = s.get(B + "/detalleAviso/primera/343225/20260617", timeout=40)
print("DETALLE", d.status_code, len(d.text))
for m in set(re.findall(r'(?:href|src|data-[a-z-]+|onclick)="([^"]*(?:pdf|PDF|descarg|pagina)[^"]*)"', d.text)):
    print("  link:", m[:200])
for u in ["/pdf/aviso/primera/343225/20260617", "/pdf/download_aviso/primera/343225/20260617", "/pdf/linkCuerpo/primera/343225/20260617"]:
    try:
        r = s.get(B + u, timeout=40)
        print("PDF?", u, r.status_code, r.headers.get("content-type"), len(r.content), r.content[:8])
    except Exception as e:
        print("PDF?", u, "error", e)
