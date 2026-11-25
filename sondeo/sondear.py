"""Sondeo temporal de fuentes de titulos publicos (no se mergea)."""
import re, html, requests
H = {"User-Agent": "Mozilla/5.0 (compatible; gbr-biblioteca/1.0)"}
def plano(t): return re.sub(r"\s+", " ", html.unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", t, flags=re.S))).strip()
def get(u): return requests.get(u, headers=H, timeout=40)
r = get("https://www.argentina.gob.ar/normativa?jurisdiccion=nacional&texto=letras%20del%20tesoro%20capitalizables&tipo_norma=resolucion%20conjunta")
t = r.text
i = t.find("norma-432388")
print("=== BLOQUE RESULTADO ===\n", t[i-1500:i+2500])
print("=== PAGINACION ===", sorted(set(re.findall(r'href="([^"]*(?:page|pagina|limit|offset)=[^"]*)"', t)))[:20])
print("=== total ===", re.findall(r"(\d[\d.]*)\s+(?:resultados|normas)", plano(t))[:5])
for u in ["https://www.argentina.gob.ar/normativa/nacional/norma-426756/texto", "https://www.argentina.gob.ar/normativa/nacional/norma-344092/texto", "https://www.argentina.gob.ar/node/483594"]:
    p = plano(get(u).text)
    k = max(0, p.find("Visto") if "Visto" in p else p.find("VISTO"))
    print(f"\n=== {u} ===\n", p[k:k+3500])
r = get("https://www.argentina.gob.ar/normativa?jurisdiccion=nacional&tipo_norma=resolucion%20conjunta&texto=deuda%20publica&page=2")
print("\n=== page=2 ===", r.status_code, r.url, re.findall(r"norma-\d+", r.text)[:12])
r = get("https://www.argentina.gob.ar/normativa?jurisdiccion=nacional&tipo_norma=resolucion%20conjunta&texto=deuda%20publica&limit=50&offset=50")
print("=== offset ===", r.status_code, r.url, re.findall(r"norma-\d+", r.text)[:12])
