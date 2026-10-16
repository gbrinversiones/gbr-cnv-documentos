"""Linea de comandos:
  python -m cnv sync [--desde 2023-01-01] [--tipos hechos,estados_contables] [--fragmento 0/4] [--tiempo-max 18000] [--max-docs N] [--solo CUIT]
  python -m cnv indice
"""
import argparse
import datetime as dt
import sys
import time

from . import almacen as alm
from .sincronizar import TIPOS_DEFECTO, cargar_universo, construir_indice_global, sincronizar_empresa


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
    sub.add_parser("indice")
    a = p.parse_args(argv)

    almacen = alm.almacen_desde_entorno()
    universo = cargar_universo()
    if a.cmd == "indice":
        print(f"indice global: {construir_indice_global(almacen, universo)} documentos")
        return 0

    i, n = (int(x) for x in a.fragmento.split("/"))
    empresas = [e for k, e in enumerate(universo) if k % n == i and (not a.solo or e["cuit"] == a.solo.replace("-", ""))]
    tipos = tuple(t for t in a.tipos.split(",") if t)
    desde = dt.date.fromisoformat(a.desde)
    limite = time.monotonic() + a.tiempo_max
    total = {"nuevos": 0, "fallidos": 0, "pendientes": 0, "empresas_con_error": 0}
    for e in empresas:
        if time.monotonic() > limite:
            print("corte por tiempo: el resto queda para la proxima corrida")
            break
        try:
            r = sincronizar_empresa(almacen, e, desde, tipos, a.trabajadores, a.max_docs, limite)
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
