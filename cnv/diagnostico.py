"""`python -m cnv diagnostico`: por que quedan documentos sin bajar / fallidos / estados contables desactualizados. Solo lee el almacen (y,
con --en-vivo, la lista publica de la CNV); no escribe nada ni usa el modelo. Imprime lineas `DIAG ...` para leerlas en el log del job."""
import collections
import datetime as dt
import re

from . import almacen as alm
from .financieros import entidad_ajena, leer_financieros
from .sincronizar import MAX_INTENTOS, TIPOS_TEXTO


def motivo_corto(error: str) -> str:
    """Agrupa los mensajes de error: sin numeros variables ni nombres de archivo, para poder contar causas."""
    m = re.sub(r"\d+", "N", (error or "sin motivo registrado").lower())
    m = re.sub(r"[\w\-. ]+\.(pdf|zip)", "ARCHIVO", m)
    return m[:70]


def estado_doc(m: dict) -> str:
    if m.get("texto_ok"):
        return "con_texto"
    i = m.get("intentos", 0)
    return "sin_bajar" if i == 0 else "agotado" if i >= MAX_INTENTOS else "reintentando"


def documentos(almacen, empresas: list, cuits_con_ons: set) -> dict:
    """Cuenta los documentos de texto sin texto por (estado, tipo, emisor con ONs, motivo)."""
    cuenta = collections.Counter()
    muestras = collections.defaultdict(list)
    por_empresa = collections.Counter()
    for e in empresas:
        for m in alm.leer_indice_empresa(almacen, e["cuit"]):
            if m.get("t") not in TIPOS_TEXTO or m.get("texto_ok"):
                continue
            est = estado_doc(m)
            extra = "escaneado" if m.get("escaneado") else ""
            motivo = motivo_corto(m.get("error")) if est != "sin_bajar" else "-"
            cuenta[(est, m["t"], "ONs" if e["cuit"] in cuits_con_ons else "sin_ONs", motivo, extra)] += 1
            por_empresa[(e["empresa"], est)] += 1
            if len(muestras[(est, m["t"], motivo)]) < 3:
                muestras[(est, m["t"], motivo)].append(f"{e['cuit']}/{m['id']}@{m.get('f')} p={m.get('p')} n={m.get('n')} i={m.get('intentos', 0)}")
    return {"cuenta": cuenta, "por_empresa": por_empresa, "muestras": muestras}


def banderas_ec(almacen, empresas: list) -> collections.Counter:
    """Estados contables CON texto por bandera: sirve para ver cuantos son imagen y que paso con el OCR."""
    c = collections.Counter()
    for e in empresas:
        fin = leer_financieros(almacen, e["cuit"])["procesados"]
        for m in alm.leer_indice_empresa(almacen, e["cuit"]):
            if m.get("t") == "estados_contables" and m.get("texto_ok"):
                p = fin.get(m["id"]) or {}
                c[("ocr" if m.get("ocr") else "ocr_intentado" if m.get("ocr_intentado") else "escaneado" if m.get("escaneado") else "texto", p.get("estado", "sin_procesar"))] += 1
    return c


def no_es_estado(almacen, empresas: list, max_muestras: int = 40) -> tuple:
    """(Counter por forma de la descripcion, muestras) de los estados contables que el extractor descarto como `no_es_estado_contable`."""
    forma, muestras = collections.Counter(), []
    for e in empresas:
        fin = leer_financieros(almacen, e["cuit"])["procesados"]
        for m in alm.leer_indice_empresa(almacen, e["cuit"]):
            if m.get("t") != "estados_contables" or (fin.get(m["id"]) or {}).get("estado") != "no_es_estado_contable":
                continue
            d = m.get("d") or ""
            clave = ("RELAC" if d.startswith("RELAC") else "NORMA/BALANCE" if d.startswith("NORMA CONTABLE") else d[:25]) + ("|ajeno" if entidad_ajena(m, e) else "|propio")
            forma[clave] += 1
            if clave.endswith("propio") and len(muestras) < max_muestras and (m.get("f") or "") >= "2026-01-01":
                texto = " ".join((alm.leer_texto(almacen, e["cuit"], m["id"]) or "")[:700].split())
                muestras.append(f"{e['empresa'][:25]} {m['id']}@{m.get('f')} p={m.get('p')} n={m.get('n')} d={d[:110]!r} T={texto[:260]!r}")
    return forma, muestras


def estado_contable(almacen, e: dict, hoy: dt.date) -> dict:
    """Ultimo estado contable del indice vs ultimo con cifras: donde se cae la cadena (indexado -> con texto -> con cifras)."""
    indice = alm.leer_indice_empresa(almacen, e["cuit"])
    fin = leer_financieros(almacen, e["cuit"])
    ecs = sorted((m for m in indice if m.get("t") == "estados_contables"), key=lambda m: (m.get("f") or "", int(m["id"])), reverse=True)
    ultimo_ec = ecs[0] if ecs else None
    con_cifras = max((p["periodo"] for p in fin["periodos"] if p.get("periodo")), default=None)
    recientes = []
    for m in ecs[:6]:
        p = fin["procesados"].get(m["id"]) or {}
        destino = ("ajeno" if entidad_ajena(m, e) else "ocr" if m.get("ocr") else "sin_texto:" + estado_doc(m) if not m.get("texto_ok")
                   else "sin_procesar" if not p else f"{p.get('estado')}:{p.get('error', '')[:40]}" if p.get("estado") != "ok" else "ok")
        recientes.append(f"{m.get('f')}={destino}")
    return {"ec_indexados": len(ecs), "ultimo_ec_indexado": ultimo_ec.get("f") if ultimo_ec else None, "ultimo_periodo_con_cifras": con_cifras, "recientes": recientes}


def imprimir(almacen, empresas: list, cuits_con_ons: set, desactualizadas: list, hoy: dt.date, en_vivo=None) -> None:
    d = documentos(almacen, empresas, cuits_con_ons)
    for (est, tipo, ons, motivo, extra), n in sorted(d["cuenta"].items(), key=lambda x: -x[1]):
        print(f"DIAG doc {n:5d} {est:12s} {tipo:18s} {ons:8s} {extra:9s} {motivo}")
    for (est, tipo, motivo), lista in sorted(d["muestras"].items()):
        if est != "sin_bajar":
            print(f"DIAG muestra {est} {tipo} {motivo}: " + " ".join(lista))
    for (bandera, est), n in sorted(banderas_ec(almacen, empresas).items(), key=lambda x: -x[1]):
        print(f"DIAG ec_con_texto {n:5d} {bandera:14s} financieros={est}")
    forma, muestras = no_es_estado(almacen, empresas)
    for k, n in forma.most_common():
        print(f"DIAG no_es_ec {n:5d} {k}")
    for x in muestras:
        print("DIAG no_es_ec_muestra " + x)
    pe = collections.Counter()
    for (emp, est), n in d["por_empresa"].items():
        pe[emp] += n
    print("DIAG empresas con mas documentos sin texto: " + "; ".join(f"{k} ({v})" for k, v in pe.most_common(25)))
    for e in desactualizadas:
        r = estado_contable(almacen, e, hoy)
        vivo = ""
        if en_vivo:
            try:
                vivo = f" cnv_ultimo_ec={en_vivo(e['cuit'])}"
            except Exception as error:
                vivo = f" cnv_error={type(error).__name__}"
        print(f"DIAG ec {e['empresa'][:38]:38s} idx={r['ec_indexados']} ultimo_idx={r['ultimo_ec_indexado']} periodo_cifras={r['ultimo_periodo_con_cifras']}{vivo} :: " + " ".join(r["recientes"]))


def probar_modelo(almacen, empresas: list, cliente, cuantos: int) -> None:
    """Vuelve a pedirle al modelo (SIN guardar nada) unos estados contables descartados como `no_es_estado_contable`, uno por empresa,
    e imprime que contesta y que paginas recibio: sirve para saber si el descarte es del documento o del extractor. Gasta ~US$ 0,01 por documento."""
    from . import financieros as f
    vistos = set()
    for e in empresas:
        if len(vistos) >= cuantos:
            break
        fin = leer_financieros(almacen, e["cuit"])["procesados"]
        for m in sorted(alm.leer_indice_empresa(almacen, e["cuit"]), key=lambda m: m.get("f") or "", reverse=True):
            if m.get("t") != "estados_contables" or (fin.get(m["id"]) or {}).get("estado") != "no_es_estado_contable" or entidad_ajena(m, e):
                continue
            if not (m.get("d") or "").startswith("NORMA CONTABLE") or e["cuit"] in vistos:
                continue
            texto = alm.leer_texto(almacen, e["cuit"], m["id"]) or ""
            recorte, paginas = f.armar_recorte(texto)
            try:
                r = f.extraer_con_claude(recorte, cliente)
                r = {k: v for k, v in r.items() if k != "cifras"} | {"n_cifras": len(r.get("cifras") or [])}
            except Exception as error:
                r = {"error": f"{type(error).__name__}: {error}"[:200]}
            print(f"DIAG modelo {e['empresa'][:25]} {m['id']}@{m.get('f')} paginas={paginas} recorte={len(recorte)} -> {r}")
            vistos.add(e["cuit"])
            break
