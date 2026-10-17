"""Los tests existentes asumen el orden secuencial (por ejemplo, un doble de la API que devuelve las respuestas en el orden de las llamadas): se fija
CNV_CONCURRENCIA=1, que tiene que dar exactamente el comportamiento de siempre. Los tests de concurrencia fijan el valor que necesiten."""
import pytest

from cnv import concurrencia


@pytest.fixture(autouse=True)
def _sin_paralelismo_por_defecto(monkeypatch):
    monkeypatch.setenv("CNV_CONCURRENCIA", "1")


@pytest.fixture(autouse=True)
def _presupuesto_holgado(monkeypatch):
    """Los tests no esperan por el presupuesto de tokens (CNV_TPM) salvo los que lo prueban a proposito; cada test arranca con una ventana vacia."""
    monkeypatch.setenv("CNV_TPM", "1000000000")
    monkeypatch.setattr(concurrencia, "_presupuesto", None)
