"""Tiempo por paso del job: cada comando de `python -m cnv` anota `comando<TAB>segundos` en el archivo de CNV_TIEMPOS (ver cnv/__main__.py::main);
`python -m cnv tiempos` junta esas filas en una tabla Markdown para el resumen del job. Sin red ni almacen."""
import os


def leer(ruta: str) -> list:
    """[(comando, segundos)] en el orden en que se corrieron. Las lineas mal formadas se ignoran."""
    if not ruta or not os.path.exists(ruta):
        return []
    salida = []
    with open(ruta, encoding="utf-8") as f:
        for linea in f:
            partes = linea.rstrip("\n").split("\t")
            if len(partes) == 2:
                try:
                    salida.append((partes[0], float(partes[1])))
                except ValueError:
                    continue
    return salida


def duracion(seg: float) -> str:
    seg = int(round(seg))
    return f"{seg // 60} min {seg % 60:02d} s" if seg >= 60 else f"{seg} s"


def a_markdown(filas: list) -> str:
    """Tabla por comando (suma de todas sus corridas en el job, con la cantidad de veces), de mas a menos tiempo."""
    if not filas:
        return "## Tiempo por paso\n\n_sin datos (CNV_TIEMPOS no estaba definido o el archivo esta vacio)_\n"
    total = sum(s for _, s in filas)
    por = {}
    for cmd, seg in filas:
        t, n = por.get(cmd, (0.0, 0))
        por[cmd] = (t + seg, n + 1)
    L = ["## Tiempo por paso", "", f"Total de los comandos del job: **{duracion(total)}**.", "", "| Comando | Veces | Tiempo | % |", "|---|--:|--:|--:|"]
    for cmd, (t, n) in sorted(por.items(), key=lambda kv: -kv[1][0]):
        L.append(f"| {cmd} | {n} | {duracion(t)} | {100 * t / total:.0f} % |")
    return "\n".join(L) + "\n"
