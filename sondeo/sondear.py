"""Sondeo temporal de fuentes de titulos publicos (no se mergea)."""
import re, html, requests, json
H = {"User-Agent": "Mozilla/5.0 (compatible; gbr-biblioteca/1.0)"}
def plano(t): return re.sub(r"\s+", " ", html.unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", t, flags=re.S))).strip()
s = requests.Session(); s.headers.update(H)
r = s.get("https://www.boletinoficial.gob.ar/seccion/primera/20260617", timeout=40)
t = r.text
print("=== LISTADO", r.status_code, r.url, len(t))
i = t.find("FINANZAS")
print(t[max(0,i-2500):i+1500])
links = re.findall(r'href="(/detalleAviso/primera/\d+/\d+)"', t)
print("n avisos", len(set(links)))
# detalle del aviso que menciona finanzas
m = re.search(r'href="(/detalleAviso/primera/\d+/\d+)"[^>]*>(?:(?!href=).){0,1500}?FINANZAS', t, re.S)
if m:
    d = s.get("https://www.boletinoficial.gob.ar" + m.group(1), timeout=40)
    p = plano(d.text)
    k = p.find("Visto") if "Visto" in p else p.find("VISTO")
    print("=== DETALLE", m.group(1), d.status_code, len(d.text))
    print(p[max(0,k-600):k+200])
    print("... RESUELVEN:", p[p.find("RESUELVEN"):p.find("RESUELVEN")+2500])
    print("pdfs:", re.findall(r'href="([^"]+\.pdf[^"]*)"', d.text)[:5], re.findall(r'href="(/pdf/aviso/[^"]+)"', d.text)[:5])
# busqueda avanzada
for url, data in [("https://www.boletinoficial.gob.ar/busquedaAvanzada/realizarBusqueda", {"params": json.dumps({"busquedaRubro": False, "hayMasResultadosBusqueda": True, "ejecutandoLlamadaAsincronicaBusqueda": False, "ultimaSeccion": "", "filtroPorRubrosSeccion": False, "filtroPorRubroBusqueda": False, "filtroPorSeccionBusqueda": False, "busquedaOriginal": True, "ordenamientoSegunda": False, "seccionesOriginales": [1], "ultimoItemExterno": None, "ultimoItemInterno": None, "texto": "Secretaría de Finanzas Letras del Tesoro", "rubros": [], "nroNorma": "", "anioNorma": "", "denominacion": "", "tipoContratacion": "", "anioContratacion": "", "nroContratacion": "", "fechaDesde": "01/01/2025", "fechaHasta": "31/01/2025", "todasLasPalabras": True, "comienzaDenominacion": True, "seccion": [1], "tipoBusqueda": "Avanzada", "numeroPagina": 1, "ultimoRubro": ""}), "array_volver": "[]"})]:
    try:
        b = s.post(url, data=data, timeout=40, headers={"X-Requested-With": "XMLHttpRequest", "Referer": "https://www.boletinoficial.gob.ar/busquedaAvanzada/primera"})
        print("=== BUSQUEDA", b.status_code, len(b.text)); print(b.text[:3000])
    except Exception as e: print("busqueda error", e)
