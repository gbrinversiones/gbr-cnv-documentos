"""Linea de comandos:
  python -m cnv sync [--desde 2023-01-01] [--tipos hechos,estados_contables,prospectos,avisos_pago,actas] [--fragmento 0/4] [--tiempo-max 18000]
                     [--max-docs N] [--solo CUIT] [--max-paginas 400] [--max-caracteres 1500000] [--max-mb 80]
  python -m cnv indice
  python -m cnv financieros [--max 150] [--solo CUIT] [--tiempo-max 1200]
  python -m cnv universo --verificar [--salida informe.json] [--tiempo-max 1800] [--refrescar]   # no modifica el CSV
  python -m cnv universo --sugerir NOMBRE [--sugerir OTRO ...] [--confirmar]
  python -m cnv salud [--formato md|json] [--dias-silencio 60] [--hoy AAAA-MM-DD]   # informe de salud (solo lee)
  python -m cnv escaneados                       # cuenta los PDF escaneados segun los marcadores (solo lee)
  python -m cnv ocr [--max-docs N] [--tiempo-max 7200]   # OCR de los escaneados; requiere tesseract (ver cnv/ocr.py)
  python -m cnv clasificar [--max 500] [--tiempo-max 900]
  python -m cnv avisos-pago [--max 200] [--solo CUIT] [--tiempo-max 900]
"""
import argparse
import dataclasses
import datetime as dt
import json
import os
import sys
import time

from . import almacen as alm
from . import avisos_pago as avp
from . import clasificacion as cla
from . import escaneados as esc
from . import ocr
from . import salud
from . import universo as uni
from . import financieros as fin
from . import resumenes as res
from .descarga import Limites
from .sincronizar import (TIPOS_DEFECTO, TIPOS_TEXTO, cargar_universo, construir_indice_global, ocr_formularios, reprocesar_escaneados,
                          sincronizar_empresa)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cnv")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--desde", default="2023-01-01")
    s.add_argument("--tipos", default=",".join(TIPOS_DEFECTO))
    s.add_argument("--fragmento", default="0/1", help="i/n: procesa solo las empresas cuyo orden % n == i (para correr en paralelo)")
    s.add_argument("--tiempo-max", type=int, default=18000, help="segundos; corta limpio y deja el resto para la proxima corrida")
    s.add_argument("--max-docs", type=int, default=None, help="tope de documentos por empresa y por corrida")
    s.add_argument("--solo", default=None, help="CUIT: procesa solo esa empresa")
    s.add_argument("--trabajadores", type=int, default=3)
    s.add_argument("--max-paginas", type=int, default=None, help="tope de paginas de texto por documento (defecto 400, o CNV_MAX_PAGINAS)")
    s.add_argument("--max-caracteres", type=int, default=None, help="tope de caracteres por documento (defecto 1.500.000, o CNV_MAX_CARACTERES)")
    s.add_argument("--max-mb", type=int, default=None, help="tamano maximo del archivo a bajar (defecto 80, o CNV_MAX_MB)")
    sub.add_parser("indice")
    rs = sub.add_parser("resumenes")
    rs.add_argument("--max", type=int, default=300, help="maximo de hechos resumidos por corrida (controla el costo)")
    rs.add_argument("--tiempo-max", type=int, default=1500)
    f = sub.add_parser("financieros")
    f.add_argument("--max", type=int, default=150, help="maximo de documentos enviados al modelo por corrida (controla el costo)")
    f.add_argument("--solo", default=None, help="CUIT: procesa solo esa empresa")
    f.add_argument("--tiempo-max", type=int, default=1200, help="segundos; corta limpio y deja el resto para la proxima corrida")
    un = sub.add_parser("universo")
    un.add_argument("--verificar", action="store_true", help="confirma cada CUIT contra la pagina de la CNV y reporta discrepancias")
    un.add_argument("--sugerir", action="append", default=[], metavar="NOMBRE", help="lista candidatos del autocompletado de la CNV (se puede repetir)")
    un.add_argument("--confirmar", action="store_true", help="con --sugerir: abre la pagina de cada candidato para confirmar su razon social")
    un.add_argument("--salida", default=None, help="archivo JSON del informe (tambien sirve de avance para retomar)")
    un.add_argument("--tiempo-max", type=int, default=1800)
    un.add_argument("--refrescar", action="store_true", help="ignora el avance guardado y verifica todo de nuevo")
    sub.add_parser("escaneados")
    sa = sub.add_parser("salud")
    sa.add_argument("--formato", choices=("md", "json"), default="md")
    sa.add_argument("--dias-silencio", type=int, default=salud.DIAS_SILENCIO)
    sa.add_argument("--hoy", default=None, help="AAAA-MM-DD (para reproducir un informe)")
    sa.add_argument("--max-fallidos", type=int, default=30)
    oc = sub.add_parser("ocr")
    oc.add_argument("--max-docs", type=int, default=None, help="tope de documentos por empresa y por corrida")
    oc.add_argument("--tiempo-max", type=int, default=7200)
    oc.add_argument("--solo", default=None, help="CUIT: procesa solo esa empresa")
    cl = sub.add_parser("clasificar")
    cl.add_argument("--max", type=int, default=500, help="maximo de hechos clasificados por corrida (controla el costo)")
    cl.add_argument("--tiempo-max", type=int, default=900)
    ap = sub.add_parser("avisos-pago")
    ap.add_argument("--max", type=int, default=200, help="maximo de avisos enviados al modelo por corrida (controla el costo)")
    ap.add_argument("--solo", default=None, help="CUIT: procesa solo esa empresa")
    ap.add_argument("--tiempo-max", type=int, default=900)
    ap.add_argument("--contar-formularios", action="store_true", help="solo lee: cuenta los avisos de pago que son formularios sin valores y como quedaron")
    a = p.parse_args(argv)

    if a.cmd == "universo":  # no usa el almacen: solo el CSV y la CNV
        if not a.verificar and not a.sugerir:
            print("indicar --verificar o --sugerir NOMBRE")
            return 2
        completo = salud.leer_universo_completo()
        if a.verificar:
            informe = uni.verificar(completo, ruta_avance=a.salida, hasta_t=time.monotonic() + a.tiempo_max, refrescar=a.refrescar)
            print(uni.a_markdown_verificacion(informe))
        for nombre in a.sugerir:
            print(uni.a_markdown_sugerencias(uni.sugerir(nombre, completo, confirmar=a.confirmar)))
        return 0

    almacen = alm.almacen_desde_entorno()
    universo = cargar_universo()
    if a.cmd == "indice":
        print(f"indice global: {construir_indice_global(almacen, universo)} documentos")
        return 0

    if a.cmd == "salud":
        informe = salud.calcular(almacen, salud.leer_universo_completo(), dt.date.fromisoformat(a.hoy) if a.hoy else None, a.dias_silencio)
        print(json.dumps(informe, ensure_ascii=False, indent=1) if a.formato == "json" else salud.a_markdown(informe, a.max_fallidos))
        return 0

    if a.cmd == "escaneados":
        print(json.dumps(esc.contar(almacen, universo), ensure_ascii=False, indent=1))
        return 0

    if a.cmd == "ocr":
        os.environ["CNV_OCR"] = "1"  # pedir este comando ya es la activacion explicita
        try:
            ocr.comprobar(ocr.configuracion()["idioma"])
        except ocr.OcrNoDisponible as error:
            print(f"OCR no disponible ({error}). Instalar tesseract-ocr, tesseract-ocr-spa, poppler-utils y requirements-ocr.txt")
            return 2
        limite = time.monotonic() + a.tiempo_max
        total = {"ocr": 0, "sin_texto": 0, "pendientes": 0}
        for e in universo:
            if (a.solo and e["cuit"] != a.solo.replace("-", "")) or time.monotonic() > limite:
                continue
            r = reprocesar_escaneados(almacen, e, Limites.desde_entorno(), limite, a.max_docs)
            for k in total:
                total[k] += r[k]
            if r["ocr"] or r["sin_texto"]:
                print(f"  {e['empresa']}: {r['ocr']} con texto por OCR, {r['sin_texto']} sin texto", flush=True)
            f = ocr_formularios(almacen, e, Limites.desde_entorno(), limite, a.max_docs)
            if f["leidos"] or f["sin_texto"]:
                print(f"  {e['empresa']}: {f['leidos']} formularios de aviso de pago leidos por OCR, {f['sin_texto']} sin valores legibles", flush=True)
        print("TOTAL", total)
        print(f"indice global: {construir_indice_global(almacen, universo)} documentos")
        return 0

    if a.cmd == "resumenes":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("falta ANTHROPIC_API_KEY")
            return 2
        import anthropic

        total = res.ejecutar(almacen, universo, anthropic.Anthropic(timeout=60.0), a.max, time.monotonic() + a.tiempo_max)
        print("TOTAL", total)
        print(f"indice global: {construir_indice_global(almacen, universo)} documentos")
        return 0

    if a.cmd == "clasificar":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("falta ANTHROPIC_API_KEY")
            return 2
        import anthropic

        total = cla.ejecutar(almacen, universo, anthropic.Anthropic(timeout=60.0), a.max, time.monotonic() + a.tiempo_max)
        print("TOTAL", total)
        print(f"indice global: {construir_indice_global(almacen, universo)} documentos")
        return 0

    if a.cmd == "avisos-pago" and a.contar_formularios:
        print(json.dumps(avp.contar_formularios(almacen, [e for e in universo if not a.solo or e["cuit"] == a.solo.replace("-", "")]), ensure_ascii=False, indent=1))
        return 0

    if a.cmd == "avisos-pago":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("falta ANTHROPIC_API_KEY")
            return 2
        import anthropic

        empresas = [e for e in universo if not a.solo or e["cuit"] == a.solo.replace("-", "")]
        total = avp.ejecutar(almacen, empresas, anthropic.Anthropic(timeout=120.0), a.max, time.monotonic() + a.tiempo_max)
        print("TOTAL", total)
        print(f"indice avisos de pago: {avp.construir_indice(almacen, universo)} pagos")
        return 0

    if a.cmd == "financieros":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("falta ANTHROPIC_API_KEY")
            return 2
        import anthropic

        empresas = [e for e in universo if not a.solo or e["cuit"] == a.solo.replace("-", "")]
        cliente = anthropic.Anthropic(timeout=120.0)
        total = fin.ejecutar(almacen, empresas, cliente, a.max, time.monotonic() + a.tiempo_max)
        print("TOTAL", total)
        print(f"indice financieros: {fin.construir_indice_financieros(almacen, universo)} periodos")
        return 0

    i, n = (int(x) for x in a.fragmento.split("/"))
    empresas = [e for k, e in enumerate(universo) if k % n == i and (not a.solo or e["cuit"] == a.solo.replace("-", ""))]
    tipos = tuple(t.strip() for t in a.tipos.split(",") if t.strip())
    desconocidos = [t for t in tipos if t not in TIPOS_TEXTO]
    if desconocidos:
        print(f"tipos desconocidos: {', '.join(desconocidos)} (validos: {', '.join(TIPOS_TEXTO)})")
        return 2
    topes = {"paginas": a.max_paginas, "caracteres": a.max_caracteres, "mb": a.max_mb}
    limites = dataclasses.replace(Limites.desde_entorno(), **{k: v for k, v in topes.items() if v and v > 0})
    desde = dt.date.fromisoformat(a.desde)
    limite = time.monotonic() + a.tiempo_max
    total = {"nuevos": 0, "fallidos": 0, "pendientes": 0, "empresas_con_error": 0}
    for e in empresas:
        if time.monotonic() > limite:
            print("corte por tiempo: el resto queda para la proxima corrida")
            break
        try:
            r = sincronizar_empresa(almacen, e, desde, tipos, a.trabajadores, a.max_docs, limite, limites)
        except Exception as error:
            total["empresas_con_error"] += 1
            print(f"  {e['empresa']}: ERROR {error}", flush=True)
            continue
        for k in ("nuevos", "fallidos", "pendientes"):
            total[k] += r[k]
        print(f"  {e['empresa']}: +{r['nuevos']} nuevos, {r['fallidos']} fallidos, {r['pendientes']} pendientes", flush=True)
    print("TOTAL", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
