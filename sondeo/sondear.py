"""Sondeo temporal: condiciones de emision (CNV) de 12 ON para cruzar con el sitio (no se mergea)."""
import csv, gzip, json, os, sys
sys.path.insert(0, ".")
from cnv import almacen as alm
T = "TTCAO YM44O YMCXO VSCXO YM34O MGCRO BF39O PNRCO BYY3O AERBO VSCMO BY34O".split()
a = alm.almacen_desde_entorno()
idx = json.loads(gzip.decompress(a.leer("cnv/indice/condiciones.json.gz")))
ons = {r["ticker"]: r for r in csv.DictReader(open("data/ons.csv", encoding="utf-8"))}
salida = {"ons": {t: idx["ons"].get(t) for t in T}, "filas": [f for f in idx["filas"] if f.get("ticker") in T], "emisiones": {}, "docs": {}}
for t in T:
    cuit = (ons.get(t) or {}).get("cuit")
    if not cuit:
        continue
    venc = ons[t]["vence"]
    crudo = a.leer(f"cnv/condiciones/{cuit}.json")
    if crudo:
        em = json.loads(crudo).get("emisiones") or []
        salida["emisiones"][t] = [e for e in em if (e.get("fecha_vencimiento") or {}).get("valor", e.get("fecha_vencimiento")) == venc or t in json.dumps(e, ensure_ascii=False)]
    docs = alm.leer_indice_empresa(a, cuit) or []
    salida["docs"][t] = [{k: d.get(k) for k in ("id", "fecha", "seccion", "titulo", "url", "texto_ok")} for d in docs
                         if any(x in (d.get("seccion") or "").lower() for x in ("resultado", "suplemento", "suscrip")) and (d.get("fecha") or "") >= "2019"][:60]
os.makedirs("salida", exist_ok=True)
json.dump(salida, open("salida/ons.json", "w"), ensure_ascii=False)
print("SALIDA_JSON_GZ_B64")
import base64
print(base64.b64encode(gzip.compress(json.dumps(salida, ensure_ascii=False).encode())).decode())
print("FIN_SALIDA")
