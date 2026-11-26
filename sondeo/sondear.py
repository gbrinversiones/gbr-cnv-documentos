"""Sondeo temporal: cupon y amortizacion de 12 ON desde sus documentos de la CNV, con citas verificadas (no se mergea)."""
import base64, csv, gzip, json, re, sys
sys.path.insert(0, ".")
import anthropic
from cnv import almacen as alm
from cnv.modelo import pedir_json
T = "TTCAO YM44O YMCXO VSCXO YM34O MGCRO BF39O PNRCO BYY3O AERBO VSCMO BY34O".split()
a = alm.almacen_desde_entorno()
idx = json.loads(gzip.decompress(a.leer("cnv/indice/condiciones.json.gz")))
cli = anthropic.Anthropic()
SIS = ("Sos analista de renta fija. Del documento (aviso de resultado, suplemento o aviso de suscripcion de una obligacion negociable argentina) "
       "extrae las condiciones de la clase indicada. Cada dato con una cita copiada LITERAL del documento (maximo 60 palabras). Si un dato no esta, null. "
       "amortizacion: lista de pagos de capital con fecha AAAA-MM-DD y porcentaje del valor nominal; si es un solo pago al vencimiento, un elemento con 100. "
       "Si el documento corresponde a una clase adicional/reapertura, las condiciones son las de la clase original que repite el documento.")
ESQ = {"type": "object", "additionalProperties": False, "required": ["tasa_anual_pct", "cita_tasa", "frecuencia", "cita_frecuencia", "primera_fecha_interes",
       "cita_primera_fecha", "amortizacion", "cita_amortizacion", "fecha_vencimiento"],
       "properties": {"tasa_anual_pct": {"type": ["number", "null"]}, "cita_tasa": {"type": ["string", "null"]},
                      "frecuencia": {"type": "string", "enum": ["mensual", "trimestral", "semestral", "anual", "al_vencimiento", "no_figura"]},
                      "cita_frecuencia": {"type": ["string", "null"]}, "primera_fecha_interes": {"type": ["string", "null"]},
                      "cita_primera_fecha": {"type": ["string", "null"]}, "fecha_vencimiento": {"type": ["string", "null"]},
                      "amortizacion": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["fecha", "porcentaje"],
                                       "properties": {"fecha": {"type": "string"}, "porcentaje": {"type": "number"}}}},
                      "cita_amortizacion": {"type": ["string", "null"]}}}
norm = lambda s: re.sub(r"\s+", " ", (s or "")).strip().lower()
salida = {}
for t in T:
    on = idx["ons"].get(t)
    if not on:
        salida[t] = {"error": "sin condiciones en el indice CNV"}; continue
    cuit = on["cuit"]
    ids = []
    for f in (on.get("fuentes") or {}).values():
        if f.get("doc") and f["doc"] not in ids:
            ids.append(f["doc"])
    lista = alm.leer_indice_empresa(a, cuit) or []
    docs = {}
    for d in lista:
        for k in ("id", "doc_id", "documento", "uuid"):
            if d.get(k) is not None:
                docs[str(d[k])] = d
    muestra = lista[0] if lista else None
    res = []
    for doc in ids[:3]:
        texto = alm.leer_texto(a, cuit, doc) or ""
        if not texto:
            continue
        meta = docs.get(doc, {})
        entrada = f"Clase buscada: {on['campos'].get('clase')} con vencimiento {on['vence']}\n\n{texto[:45000]}"
        try:
            r = pedir_json(cli, SIS, entrada, ESQ, max_tokens=2500)
        except Exception as e:
            res.append({"doc": doc, "error": str(e)[:200]}); continue
        nt = norm(texto)
        r["citas_ok"] = {k: (norm(r.get(k)) in nt) if r.get(k) else None for k in ("cita_tasa", "cita_frecuencia", "cita_primera_fecha", "cita_amortizacion")}
        res.append({"doc": doc, "meta": {k: meta.get(k) for k in list(meta)[:12]}, **r})
    salida[t] = {"muestra": muestra, "cuit": cuit, "vence": on["vence"], "indice": on["campos"], "extraido": res}
print("SALIDA_JSON_GZ_B64")
print(base64.b64encode(gzip.compress(json.dumps(salida, ensure_ascii=False).encode())).decode())
print("FIN_SALIDA")
