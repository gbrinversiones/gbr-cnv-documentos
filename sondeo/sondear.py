"""Sondeo temporal de fuentes de titulos publicos (no se mergea)."""
import re, sys, pathlib, requests
H = {"User-Agent": "Mozilla/5.0 (compatible; gbr-biblioteca/1.0)"}
URLS = [
    "https://www.argentina.gob.ar/economia/finanzas/licitaciones",
    "https://www.argentina.gob.ar/economia/finanzas/licitaciones-de-titulos-publicos",
    "https://www.argentina.gob.ar/node/483594",
    "https://www.argentina.gob.ar/node/506056",
    "https://www.argentina.gob.ar/normativa/nacional/norma-426756/texto",
    "https://www.argentina.gob.ar/normativa/buscar?jurisdiccion=nacional&texto=letras+del+tesoro+capitalizables&tipo_norma=resolucion+conjunta",
    "https://www.argentina.gob.ar/normativa/buscar?texto=LECAP&jurisdiccion=nacional",
    "https://servicios.infoleg.gob.ar/infolegInternet/buscarNormas.do?texto=letras+del+tesoro",
    "https://www.boletinoficial.gob.ar/seccion/primera",
    "https://www.argentina.gob.ar/economia/finanzas/deudapublica",
    "https://www.argentina.gob.ar/economia/finanzas/titulos-publicos",
    "https://www.argentina.gob.ar/noticias?search_api_fulltext=llamado+a+licitacion",
    "https://www.byma.com.ar/productos/productos-financieros/titulos-publicos",
    "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free/public-bonds",
    "https://www.a3mercados.com.ar/",
    "https://efts.sec.gov/LATEST/search-index?q=%22Republic%20of%20Argentina%22&forms=424B5",
    "https://data.sec.gov/submissions/CIK0000914021.json",
]
out = pathlib.Path("salida"); out.mkdir(exist_ok=True)
for i, u in enumerate(URLS):
    try:
        r = requests.get(u, headers=H if "sec.gov" not in u else {"User-Agent": "gbr-biblioteca contacto@gbrinversiones.com.ar"}, timeout=30)
        t = r.text
        (out / f"{i:02d}.html").write_text(t)
        links = re.findall(r'href="([^"]+)"', t)
        interesantes = [l for l in links if re.search(r"node/|normativa|licitac|\.pdf|norma-|detalleAviso", l)][:25]
        print(f"\n### {i} {u}\n  status={r.status_code} len={len(t)} final={r.url}")
        print("  titulo:", (re.search(r"<title>(.*?)</title>", t, re.S) or [None, ""])[1].strip()[:120])
        for l in interesantes:
            print("   ", l[:160])
    except Exception as e:
        print(f"\n### {i} {u}\n  ERROR {e}")
