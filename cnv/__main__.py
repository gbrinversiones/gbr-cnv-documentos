"""Linea de comandos:
  python -m cnv sync [--desde 2023-01-01] [--tipos hechos,estados_contables,prospectos,avisos_pago,actas] [--fragmento 0/4] [--tiempo-max 18000]
                     [--max-docs N] [--solo CUIT] [--max-paginas 400] [--max-caracteres 1500000] [--max-mb 80]
  python -m cnv indice
  python -m cnv financieros [--max 150] [--solo CUIT] [--tiempo-max 1200]
  python -m cnv avisos-pago [--max 200] [--solo CUIT] [--tiempo-max 900]
"""
import argparse
import dataclasses
import datetime as dt
import os
import sys
import time

from . import almacen as alm
from . import avisos_pago as avp
from . import financieros as fin
from . import resumenes as res
from .descarga import Limites
from .sincronizar import TIPOS_DEFECTO, TIPOS_TEXTO, cargar_universo, construir_indice_global, sincronizar_empresa


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
    ap = sub.add_parser("avisos-pago")
    ap.add_argument("--max", type=int, default=200, help="maximo de avisos enviados al modelo por corrida (controla el costo)")
    ap.add_argument("--solo", default=None, help="CUIT: procesa solo esa empresa")
    ap.add_argument("--tiempo-max", type=int, default=900)
    a = p.parse_args(argv)

    almacen = alm.almacen_desde_entorno()
    universo = cargar_universo()
    if a.cmd == "indice":
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
