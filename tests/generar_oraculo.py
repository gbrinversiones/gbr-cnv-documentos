"""Genera tests/fixtures/busqueda_oraculo.json: corpus sintetico chico + el indice que arma cnv/busqueda.py + 3 consultas con su ranking BM25.
El lado JS (Cloud Function) carga este mismo JSON y compara su ranking con `consultas[*].esperado`.
Regenerar solo si cambia a proposito el formato o el algoritmo (y subir cnv.busqueda.VERSION):  python -m tests.generar_oraculo"""
import gzip
import json
import os
import tempfile

from cnv import almacen as alm
from cnv import busqueda as bus

RUTA = os.path.join(os.path.dirname(__file__), "fixtures", "busqueda_oraculo.json")

CORPUS = [
    ("30111111112", "101", "hechos", "[pagina 1]\nLa Sociedad informa la recompra de obligaciones negociables Clase 5 por hasta US$ 5.000.000. El Directorio aprobo la recompra en el mercado."),
    ("30111111112", "102", "hechos", "[pagina 1]\nResultados del tercer trimestre: ganancia neta de $ 1.250 millones. La ganancia operativa crecio por mayores ventas de energia."),
    ("30111111112", "103", "hechos", "[pagina 1]\nConvocatoria a Asamblea General Ordinaria de accionistas para considerar la distribucion de dividendos en efectivo."),
    ("30111111112", "104", "avisos_pago", "[pagina 1]\nAviso de pago de servicios de las Obligaciones Negociables Clase 5: pago de intereses el 15 de noviembre de 2026."),
    ("30222222223", "201", "hechos", "[pagina 1]\nCalificacion de riesgo: la calificadora confirmo la calificacion AA+ de las obligaciones negociables de la compania."),
    ("30222222223", "202", "prospectos", "[pagina 1]\nPrograma global de emision de obligaciones negociables por hasta US$ 500.000.000. Factores de riesgo y condiciones de emision.\n\n[pagina 2]\nLa emision de obligaciones negociables Clase 7 devengara intereses a tasa fija."),
    ("30222222223", "203", "actas", "[pagina 1]\nActa de directorio: se designa nuevo director titular y se aprueba la recompra de acciones propias por hasta 2.000.000 de acciones."),
    ("30222222223", "204", "hechos", "[pagina 1]\nDemanda judicial: la sociedad fue notificada de una demanda por incumplimiento contractual. La sociedad considera que la demanda carece de fundamento."),
    ("30333333334", "301", "hechos", "[pagina 1]\nEl Directorio resolvio distribuir dividendos en efectivo por $ 25.000.000 a los accionistas. Pago de dividendos a partir del 20 de octubre."),
    ("30333333334", "302", "hechos", "[pagina 1]\nColocacion de obligaciones negociables Clase 3: se recibieron ofertas por US$ 30.000.000. Tasa de corte 8,5% nominal anual."),
    ("30333333334", "303", "avisos_pago", "[pagina 1]\nAviso de pago de intereses de las obligaciones negociables Clase 3. Fecha de pago: 3 de diciembre de 2026."),
    ("30333333334", "304", "hechos", "[pagina 1]\nCompra de la planta industrial: la empresa adquirio activos de energia renovable por US$ 12.000.000."),
]
CONSULTAS = ["recompra directorio aprobo acciones", "dividendos efectivo accionistas pago", "intereses pago clase fecha"]
CASOS_TOKENIZACION = ["Obligaciones NEGOCIABLES Clase 5", "Calificación de riesgo AA+ — energía", "ÁÉÍÓÚ ñandú 2026-10-15", "de la el por S.A. CNV", "a1 b22 ccc " + "x" * 31 + " " + "y" * 30]


def indice_en_memoria() -> dict:
    with tempfile.TemporaryDirectory() as d:
        a = alm.AlmacenLocal(d)
        por_cuit = {}
        for cuit, doc_id, tipo, texto in CORPUS:
            alm.guardar_texto(a, cuit, doc_id, texto)
            por_cuit.setdefault(cuit, []).append({"id": doc_id, "t": tipo, "texto_ok": True})
        for cuit, docs in por_cuit.items():
            alm.guardar_indice_empresa(a, cuit, docs)
        import datetime as dt
        bus.construir(a, [{"empresa": c, "cuit": c} for c in por_cuit], ahora=dt.datetime(2026, 10, 16, 12, 0, 0, tzinfo=dt.timezone.utc))
        leer = lambda n: json.loads(gzip.decompress(a.leer(n)))  # noqa: E731
        salida = {"meta": json.loads(a.leer("cnv/busqueda/meta.json")), "docs": leer("cnv/busqueda/docs.json.gz"),
                  "shards": {f"s{k:02d}": leer(bus.nombre_shard(k)) for k in range(bus.SHARDS) if leer(bus.nombre_shard(k))}}
        consultas = [{"consulta": q, "k": 5, "esperado": [[c, d, round(s, 6)] for c, d, s in bus.buscar_bm25(a, q, 5)]} for q in CONSULTAS]
    return {"indice": salida, "consultas": consultas}


def generar() -> dict:
    base = indice_en_memoria()
    return {
        "descripcion": "Corpus sintetico + indice generado por cnv/busqueda.py + ranking BM25 esperado (k1=1.2, b=0.75). Contrato con el lector en JS.",
        "parametros": {"k1": bus.K1, "b": bus.B, "shards": bus.SHARDS, "df_min": bus.DF_MIN, "df_max_porcentaje": bus.DF_MAX_PORCENTAJE, "tf_max": bus.TF_MAX,
                       "max_caracteres": bus.MAX_CARACTERES, "largo_token": [bus.MIN_TOKEN, bus.MAX_TOKEN]},
        "idf": "ln(1 + (N - df + 0.5) / (df + 0.5)); score = sum idf * tf*(k1+1) / (tf + k1*(1 - b + b*len/avgdl)); desempate: menor doc_idx",
        "stopwords": sorted(bus.STOPWORDS),
        "tokenizacion": [[t, bus.tokenizar(t)] for t in CASOS_TOKENIZACION],
        "shards_literales": {t: bus.shard_de(t) for t in ("hello", "hello world", "bonos", "dividendos", "obligaciones", "recompra", "energía")},
        "corpus": [{"cuit": c, "doc_id": d, "tipo": t, "texto": x} for c, d, t, x in CORPUS],
        **base,
    }


if __name__ == "__main__":
    with open(RUTA, "w", encoding="utf-8") as f:
        json.dump(generar(), f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    print("escrito", RUTA)
