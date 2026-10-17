"""Fase 23: eficiencia del job indice. Idempotencia intacta: lo que se saltea es trabajo cuyo resultado seria identico."""
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from cnv import __main__ as cli
from cnv import almacen as alm
from cnv import sincronizar as sync
from cnv import tiempos as tmp

from .test_robustez import NotFound, instalar_gcs

E1 = {"empresa": "UNO S.A.", "cuit": "30546689979"}
E2 = {"empresa": "DOS S.A.", "cuit": "30500010912"}


def doc(i, **kw):
    return {"id": str(i), "c": "x", "e": "x", "f": "2026-10-01", "t": "hechos", "s": "HR", "d": "d", "u": "u", "texto_ok": True, **kw}


@pytest.fixture
def base(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    alm.guardar_indice_empresa(a, E1["cuit"], [doc(1), doc(2)])
    alm.guardar_indice_empresa(a, E2["cuit"], [doc(3), doc(4, texto_ok=False)])
    return a


class Contador:
    """Cuenta cuantos indices por empresa se leen: el costo que el salto evita."""
    def __init__(self, a):
        self.a, self.lecturas = a, []

    def __getattr__(self, n):
        return getattr(self.a, n)

    def leer(self, nombre):
        self.lecturas.append(nombre)
        return self.a.leer(nombre)


# --- indice global ---------------------------------------------------------------------------

def test_global_se_construye_y_guarda_su_huella(base):
    assert sync.construir_indice_global(base, [E1, E2]) == 3
    d = json.loads(base.leer(sync.RUTA_FIRMA_GLOBAL))
    assert d["n"] == 3 and len(d["firma"]) == 40


def test_global_sin_cambios_no_lee_los_indices_por_empresa(base):
    sync.construir_indice_global(base, [E1, E2])
    c = Contador(base)
    assert sync.construir_indice_global(c, [E1, E2]) == 3
    assert not any(n.startswith("cnv/indice/3") for n in c.lecturas), c.lecturas          # solo leyo la huella


def test_global_se_reconstruye_si_cambia_un_indice_por_empresa(base):
    sync.construir_indice_global(base, [E1, E2])
    alm.guardar_indice_empresa(base, E1["cuit"], [doc(1), doc(2), doc(5)])
    assert sync.construir_indice_global(base, [E1, E2]) == 4


def test_global_cambia_con_el_resumen_o_la_categoria_porque_van_en_el_global(base):
    sync.construir_indice_global(base, [E1, E2])
    ind = alm.leer_indice_empresa(base, E1["cuit"])
    ind[0]["r"], ind[0]["k"] = "resumen", "dividendos"
    alm.guardar_indice_empresa(base, E1["cuit"], ind)
    sync.construir_indice_global(base, [E1, E2])
    import gzip
    filas = json.loads(gzip.decompress(base.leer("cnv/indice/global.json.gz")))
    assert any(f.get("r") == "resumen" and f.get("k") == "dividendos" for f in filas)


def test_global_distingue_otro_conjunto_de_empresas(base):
    sync.construir_indice_global(base, [E1, E2])
    assert sync.construir_indice_global(base, [E1]) == 2          # otra lista de empresas: otra huella, se reconstruye
    assert sync.construir_indice_global(base, [E1, E2]) == 3


def test_global_forzar_y_huella_o_global_ausentes_o_ilegibles(base):
    sync.construir_indice_global(base, [E1, E2])
    antes = base.leer("cnv/indice/global.json.gz")
    c = Contador(base)
    assert sync.construir_indice_global(c, [E1, E2], forzar=True) == 3 and any(n == "cnv/indice/" + E1["cuit"] + ".json" for n in c.lecturas)
    base.guardar(sync.RUTA_FIRMA_GLOBAL, b"no es json")
    assert sync.construir_indice_global(base, [E1, E2]) == 3
    base.guardar(sync.RUTA_FIRMA_GLOBAL, b'{"firma": "x"}')
    assert sync.construir_indice_global(base, [E1, E2]) == 3
    import os
    os.remove(os.path.join(base.raiz, "cnv", "indice", "global.json.gz"))
    assert sync.construir_indice_global(base, [E1, E2]) == 3 and base.leer("cnv/indice/global.json.gz") == antes      # el global faltante se recrea igual


def test_global_idempotente_mismos_bytes(base):
    sync.construir_indice_global(base, [E1, E2])
    antes = {n: base.leer(n) for n in ("cnv/indice/global.json.gz", sync.RUTA_FIRMA_GLOBAL)}
    sync.construir_indice_global(base, [E1, E2], forzar=True)
    assert {n: base.leer(n) for n in antes} == antes


# --- almacen ------------------------------------------------------------------------------------

def test_firmas_locales_solo_con_el_prefijo_y_cambian_con_el_contenido(tmp_path):
    a = alm.AlmacenLocal(str(tmp_path))
    a.guardar("cnv/indice/1.json", b"a")
    a.guardar("cnv/indice/2.json", b"b")
    a.guardar("cnv/otro/3.json", b"c")
    f = a.firmas("cnv/indice/")
    assert set(f) == {"cnv/indice/1.json", "cnv/indice/2.json"}
    a.guardar("cnv/indice/1.json", b"aa")
    assert a.firmas("cnv/indice/")["cnv/indice/1.json"] != f["cnv/indice/1.json"] and a.firmas("cnv/indice/")["cnv/indice/2.json"] == f["cnv/indice/2.json"]
    assert a.firmas("cnv/nada/") == {} and a.firmas("solo") == {}


def test_gcs_leer_es_un_solo_pedido_y_un_objeto_inexistente_es_none(monkeypatch):
    tienda, _ = instalar_gcs(monkeypatch)
    pedidos = []
    orig = sys.modules["google.cloud.storage"].Client

    class Blob:
        def __init__(self, nombre):
            self.nombre = nombre

        def download_as_bytes(self):
            pedidos.append(("descarga", self.nombre))
            if self.nombre not in tienda:
                raise NotFound(self.nombre)
            return tienda[self.nombre]

        def exists(self):
            pedidos.append(("exists", self.nombre))
            return self.nombre in tienda

    sys.modules["google.cloud.storage"].Client = lambda: SimpleNamespace(bucket=lambda n: SimpleNamespace(blob=Blob))
    try:
        a = alm.AlmacenGCS("b")
        tienda["x"] = b"1"
        assert a.leer("x") == b"1" and a.leer("y") is None
        assert pedidos == [("descarga", "x"), ("descarga", "y")]          # ningun exists(): antes eran dos pedidos por lectura
    finally:
        sys.modules["google.cloud.storage"].Client = orig


def test_gcs_otros_errores_se_propagan(monkeypatch):
    instalar_gcs(monkeypatch)

    class Blob:
        def download_as_bytes(self):
            raise PermissionError("403")

    sys.modules["google.cloud.storage"].Client = lambda: SimpleNamespace(bucket=lambda n: SimpleNamespace(blob=lambda nombre: Blob()))
    with pytest.raises(PermissionError):
        alm.AlmacenGCS("b").leer("x")


def test_gcs_firmas_lista_una_vez_y_usa_md5_o_crc32c_o_generacion(monkeypatch):
    instalar_gcs(monkeypatch)
    blobs = [SimpleNamespace(name="cnv/indice/1.json", md5_hash="AAA", crc32c="c1", generation=1),
             SimpleNamespace(name="cnv/indice/2.json", md5_hash=None, crc32c="c2", generation=2),
             SimpleNamespace(name="cnv/indice/3.json", md5_hash=None, crc32c=None, generation=3)]
    listados = []
    sys.modules["google.cloud.storage"].Client = lambda: SimpleNamespace(bucket=lambda n: SimpleNamespace(list_blobs=lambda prefix: listados.append(prefix) or blobs))
    assert alm.AlmacenGCS("b").firmas("cnv/indice/") == {"cnv/indice/1.json": "AAA", "cnv/indice/2.json": "c2", "cnv/indice/3.json": "3"} and listados == ["cnv/indice/"]


# --- tiempos ------------------------------------------------------------------------------------

def test_main_anota_el_tiempo_en_stderr_y_en_el_archivo(base, monkeypatch, capsys, tmp_path):
    ruta = tmp_path / "t.tsv"
    monkeypatch.setenv("CNV_TIEMPOS", str(ruta))
    monkeypatch.setenv("DATOS_LOCAL", str(base.raiz))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [E1, E2])
    assert cli.main(["indice"]) == 0
    sal = capsys.readouterr()
    assert "indice global: 3 documentos" in sal.out and "TIEMPO" not in sal.out and sal.err.startswith("TIEMPO indice: ")
    filas = tmp.leer(str(ruta))
    assert len(filas) == 1 and filas[0][0] == "indice" and filas[0][1] >= 0


def test_main_anota_el_tiempo_aunque_falle_y_sin_archivo_no_escribe_nada(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("CNV_TIEMPOS", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["--no-existe"])
    assert "TIEMPO ?: " in capsys.readouterr().err
    ruta = tmp_path / "t.tsv"
    monkeypatch.setenv("CNV_TIEMPOS", str(ruta))
    with pytest.raises(SystemExit):
        cli.main(["comando-que-no-existe"])
    assert tmp.leer(str(ruta)) == [("comando-que-no-existe", tmp.leer(str(ruta))[0][1])]


def test_el_archivo_de_tiempos_no_se_puede_escribir_no_rompe_el_comando(base, monkeypatch, tmp_path):
    monkeypatch.setenv("CNV_TIEMPOS", str(tmp_path / "no_existe" / "t.tsv"))
    monkeypatch.setenv("DATOS_LOCAL", str(base.raiz))
    monkeypatch.setattr(cli, "cargar_universo", lambda: [E1, E2])
    assert cli.main(["indice"]) == 0


def test_tabla_de_tiempos(tmp_path, monkeypatch, capsys):
    ruta = tmp_path / "t.tsv"
    ruta.write_text("financieros\t600.0\nauditar-cifras\t25.5\nbasura\nratios\tx\nauditar-cifras\t30.5\nbusqueda\t240\n", encoding="utf-8")
    assert tmp.leer(str(ruta)) == [("financieros", 600.0), ("auditar-cifras", 25.5), ("auditar-cifras", 30.5), ("busqueda", 240.0)]
    md = tmp.a_markdown(tmp.leer(str(ruta)))
    assert "**14 min 56 s**" in md and "| financieros | 1 | 10 min 00 s | 67 % |" in md and "| auditar-cifras | 2 | 56 s | 6 % |" in md
    assert md.index("financieros") < md.index("busqueda") < md.index("auditar-cifras")          # de mas a menos tiempo
    assert "sin datos" in tmp.a_markdown([]) and tmp.leer("") == [] and tmp.leer(str(tmp_path / "nada")) == []
    monkeypatch.setenv("CNV_TIEMPOS", str(ruta))
    assert cli.main(["tiempos"]) == 0 and "## Tiempo por paso" in capsys.readouterr().out
    assert tmp.leer(str(ruta))[-1][0] == "busqueda"          # el comando `tiempos` no se anota a si mismo
