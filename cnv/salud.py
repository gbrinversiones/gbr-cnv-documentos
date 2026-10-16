"""Informe de salud de la base: lee los indices ya guardados (no toca la red ni el modelo) y dice que falta o esta roto.

Por empresa: documentos por tipo, fecha del ultimo documento, fallidos con motivo, escaneados, y cobertura de
resumenes, categorias, cifras de estados contables y avisos de pago. Marca las empresas "silenciosas" (activas pero sin
documentos nuevos hace mas de N dias), las que no tienen ningun documento y las filas del universo sin CUIT valido.
"""
import csv
import datetime as dt
from collections import Counter

from . import almacen as alm
from .avisos_pago import leer_avisos
from .financieros import leer_financieros
from .sincronizar import MAX_INTENTOS, TIPOS_TEXTO

DIAS_SILENCIO = 60  # sin documentos nuevos hace mas que esto -> silenciosa
DIAS_ACTIVIDAD = 365  # "activa" = tuvo algun documento en el ultimo anio (si no, no se espera nada nuevo)


def cuit_valido(cuit: str) -> bool:
    """11 digitos y digito verificador correcto (modulo 11 con pesos 5,4,3,2,7,6,5,4,3,2)."""
    c = (cuit or "").replace("-", "").strip()
    if not (len(c) == 11 and c.isdigit()):
        return False
    resto = 11 - sum(int(d) * p for d, p in zip(c[:10], (5, 4, 3, 2, 7, 6, 5, 4, 3, 2))) % 11
    return int(c[10]) == {11: 0, 10: 9}.get(resto, resto)


def leer_universo_completo(ruta: str = "data/universo.csv") -> list:
    """TODAS las filas del CSV, incluso las sin CUIT (cargar_universo las saltea)."""
    with open(ruta, encoding="utf-8", newline="") as f:
        return [{"empresa": (r.get("empresa") or "").strip(), "cuit": (r.get("cuit") or "").replace("-", "").strip()} for r in csv.DictReader(f)]


def _fecha(s):
    try:
        return dt.date.fromisoformat(s) if isinstance(s, str) and s else None
    except ValueError:
        return None


def _porcentaje(parte: int, total: int):
    return round(100 * parte / total, 1) if total else None


def _empresa(almacen, e: dict, hoy: dt.date, dias_silencio: int) -> dict:
    indice = alm.leer_indice_empresa(almacen, e["cuit"])
    fin = leer_financieros(almacen, e["cuit"])["procesados"]
    avisos = leer_avisos(almacen, e["cuit"])["procesados"]
    tipos = {}
    fechas, fechas_texto = [], []
    fallidos, pendientes_bajada = [], 0
    r = {"escaneados": 0, "truncados": 0, "parciales": 0, "ocr": 0}
    hechos_texto = resumidos = clasificados = ec_texto = ec_ok = av_texto = av_ok = 0
    estados_fin = Counter()
    for m in indice:
        t = m.get("t") or "otros"
        d = tipos.setdefault(t, {"total": 0, "con_texto": 0})
        d["total"] += 1
        f = _fecha(m.get("f"))
        if f:
            fechas.append(f)
        if m.get("texto_ok"):
            d["con_texto"] += 1
            if f:
                fechas_texto.append(f)
        elif t in TIPOS_TEXTO:
            if m.get("intentos", 0) > 0:
                fallidos.append({"id": m["id"], "tipo": t, "fecha": m.get("f"), "intentos": m.get("intentos", 0),
                                 "motivo": m.get("error") or "sin motivo registrado", "rendido": m.get("intentos", 0) >= MAX_INTENTOS})
            else:
                pendientes_bajada += 1
        r["escaneados"] += bool(m.get("escaneado"))
        r["truncados"] += bool(m.get("trunc"))
        r["parciales"] += bool(m.get("parcial"))
        r["ocr"] += bool(m.get("ocr"))
        if m.get("texto_ok") and t == "hechos":
            hechos_texto += 1
            resumidos += bool(m.get("r"))
            clasificados += bool(m.get("k"))
        if m.get("texto_ok") and t == "estados_contables" and not m.get("ocr"):
            ec_texto += 1
            estado = (fin.get(m["id"]) or {}).get("estado", "pendiente")
            estados_fin[estado] += 1
            ec_ok += estado == "ok"
        if m.get("texto_ok") and t == "avisos_pago" and not m.get("ocr"):
            av_texto += 1
            av_ok += (avisos.get(m["id"]) or {}).get("estado") == "ok"
    ultimo = max(fechas, default=None)
    dias = (hoy - ultimo).days if ultimo else None
    activa = dias is not None and dias <= DIAS_ACTIVIDAD
    return {
        "empresa": e["empresa"], "cuit": e["cuit"], "tipos": tipos, "documentos": len(indice),
        "ultimo_documento": ultimo.isoformat() if ultimo else None, "ultimo_con_texto": max(fechas_texto).isoformat() if fechas_texto else None,
        "dias_sin_documentos": dias, "activa": activa, "silenciosa": bool(activa and dias > dias_silencio),
        "fallidos": sorted(fallidos, key=lambda x: x["id"], reverse=True), "pendientes_de_bajada": pendientes_bajada, **r,
        "cobertura": {
            "resumenes": {"hechos_con_texto": hechos_texto, "resumidos": resumidos, "pct": _porcentaje(resumidos, hechos_texto)},
            "categorias": {"hechos_con_texto": hechos_texto, "clasificados": clasificados, "pct": _porcentaje(clasificados, hechos_texto)},
            "cifras": {"estados_con_texto": ec_texto, "con_cifras": ec_ok, "pct": _porcentaje(ec_ok, ec_texto), "estados": dict(estados_fin)},
            "avisos_pago": {"avisos_con_texto": av_texto, "estructurados": av_ok, "pct": _porcentaje(av_ok, av_texto)},
        },
    }


def calcular(almacen, universo_completo: list, hoy: dt.date | None = None, dias_silencio: int = DIAS_SILENCIO) -> dict:
    """Informe completo (dict serializable). `universo_completo` son las filas del CSV, con o sin CUIT."""
    hoy = hoy or dt.date.today()
    sin_cuit = [x["empresa"] for x in universo_completo if not x["cuit"]]
    cuit_malo = [{"empresa": x["empresa"], "cuit": x["cuit"]} for x in universo_completo if x["cuit"] and not cuit_valido(x["cuit"])]
    empresas = [_empresa(almacen, x, hoy, dias_silencio) for x in universo_completo if x["cuit"]]
    por_tipo = Counter()
    con_texto = Counter()
    for e in empresas:
        for t, d in e["tipos"].items():
            por_tipo[t] += d["total"]
            con_texto[t] += d["con_texto"]

    def suma(campo):
        return sum(e[campo] for e in empresas)

    def cobertura(clave, parte, total):
        p, t = sum(e["cobertura"][clave][parte] for e in empresas), sum(e["cobertura"][clave][total] for e in empresas)
        return {parte: p, total: t, "pct": _porcentaje(p, t)}

    return {
        "fecha": hoy.isoformat(), "dias_silencio": dias_silencio,
        "resumen": {
            "empresas": len(empresas), "documentos": sum(e["documentos"] for e in empresas),
            "por_tipo": {t: {"total": por_tipo[t], "con_texto": con_texto[t]} for t in sorted(por_tipo)},
            "fallidos": sum(len(e["fallidos"]) for e in empresas), "fallidos_rendidos": sum(f["rendido"] for e in empresas for f in e["fallidos"]),
            "pendientes_de_bajada": suma("pendientes_de_bajada"), "escaneados": suma("escaneados"), "truncados": suma("truncados"),
            "parciales": suma("parciales"), "leidos_por_ocr": suma("ocr"),
            "cobertura": {"resumenes": cobertura("resumenes", "resumidos", "hechos_con_texto"), "categorias": cobertura("categorias", "clasificados", "hechos_con_texto"),
                          "cifras": cobertura("cifras", "con_cifras", "estados_con_texto"), "avisos_pago": cobertura("avisos_pago", "estructurados", "avisos_con_texto")},
        },
        "silenciosas": sorted(({"empresa": e["empresa"], "cuit": e["cuit"], "ultimo_documento": e["ultimo_documento"], "dias": e["dias_sin_documentos"]}
                               for e in empresas if e["silenciosa"]), key=lambda x: -x["dias"]),
        "sin_documentos": [{"empresa": e["empresa"], "cuit": e["cuit"]} for e in empresas if e["documentos"] == 0],
        "universo_sin_cuit": sin_cuit, "universo_cuit_invalido": cuit_malo,
        "empresas": sorted(empresas, key=lambda e: e["empresa"]),
    }


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------

def _pct(c) -> str:
    return "-" if c["pct"] is None else f"{c['pct']:.0f}%"


def a_markdown(inf: dict, max_fallidos: int = 30) -> str:
    """Texto en Markdown para el resumen del job de GitHub Actions ($GITHUB_STEP_SUMMARY)."""
    r = inf["resumen"]
    L = [f"## Salud de la base CNV ({inf['fecha']})", ""]
    L.append(f"**{r['empresas']}** empresas · **{r['documentos']}** documentos · {r['fallidos']} fallidos ({r['fallidos_rendidos']} rendidos tras {MAX_INTENTOS} intentos) · "
             f"{r['pendientes_de_bajada']} sin bajar · {r['escaneados']} escaneados · {r['truncados']} truncados · {r['parciales']} parciales · {r['leidos_por_ocr']} por OCR")
    L += ["", "| Tipo | Documentos | Con texto |", "|---|--:|--:|"]
    L += [f"| {t} | {d['total']} | {d['con_texto']} |" for t, d in r["por_tipo"].items()]
    c = r["cobertura"]
    L += ["", "| Cobertura | Hechos |", "|---|--:|",
          f"| Resúmenes | {c['resumenes']['resumidos']}/{c['resumenes']['hechos_con_texto']} ({_pct(c['resumenes'])}) |",
          f"| Categorías | {c['categorias']['clasificados']}/{c['categorias']['hechos_con_texto']} ({_pct(c['categorias'])}) |",
          f"| Cifras de estados contables | {c['cifras']['con_cifras']}/{c['cifras']['estados_con_texto']} ({_pct(c['cifras'])}) |",
          f"| Avisos de pago estructurados | {c['avisos_pago']['estructurados']}/{c['avisos_pago']['avisos_con_texto']} ({_pct(c['avisos_pago'])}) |"]
    L += ["", f"### Empresas silenciosas (activas, sin documentos hace más de {inf['dias_silencio']} días): {len(inf['silenciosas'])}"]
    L += [f"- {s['empresa']} ({s['cuit']}): último documento {s['ultimo_documento']} (hace {s['dias']} días)" for s in inf["silenciosas"]] or ["- ninguna"]
    L += ["", f"### Empresas sin ningún documento: {len(inf['sin_documentos'])}"]
    L += [f"- {s['empresa']} ({s['cuit']})" for s in inf["sin_documentos"]] or ["- ninguna"]
    L += ["", f"### Universo sin CUIT: {len(inf['universo_sin_cuit'])} · con CUIT inválido: {len(inf['universo_cuit_invalido'])}"]
    L += [f"- {n}: sin CUIT" for n in inf["universo_sin_cuit"]] + [f"- {x['empresa']}: CUIT {x['cuit']} no pasa el dígito verificador" for x in inf["universo_cuit_invalido"]]
    fall = [(e["empresa"], f) for e in inf["empresas"] for f in e["fallidos"]]
    L += ["", f"### Documentos fallidos ({len(fall)}, se muestran {min(len(fall), max_fallidos)})"]
    L += [f"- {n} #{f['id']} ({f['tipo']}, {f['fecha']}): {f['motivo']} — {f['intentos']} intento(s){' — RENDIDO' if f['rendido'] else ''}" for n, f in fall[:max_fallidos]] or ["- ninguno"]
    L += ["", "<details><summary>Detalle por empresa</summary>", "",
          "| Empresa | Docs | Último | Hechos | EECC | Prosp. | Pagos | Actas | Fallidos | Escan. | Resúm. | Cifras |", "|---|--:|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for e in inf["empresas"]:
        n = lambda t: e["tipos"].get(t, {}).get("con_texto", 0)  # noqa: E731
        marca = " 🔇" if e["silenciosa"] else ""
        L.append(f"| {e['empresa']}{marca} | {e['documentos']} | {e['ultimo_documento'] or '-'} | {n('hechos')} | {n('estados_contables')} | {n('prospectos')} | {n('avisos_pago')} | {n('actas')} | "
                 f"{len(e['fallidos'])} | {e['escaneados']} | {_pct(e['cobertura']['resumenes'])} | {_pct(e['cobertura']['cifras'])} |")
    L += ["", "</details>", ""]
    return "\n".join(L)
