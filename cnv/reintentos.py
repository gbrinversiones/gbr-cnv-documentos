"""`python -m cnv reintentos --reset`: devuelve a «pendiente» los documentos que quedaron marcados con error por un problema de la CUENTA o de la API
(sin saldo, clave invalida, limites de tasa, sobrecarga, corte de red) y NO por el documento.

Recorre los marcadores de los pasos con modelo (financieros, avisos-pago, condiciones, eventos; resumenes y clasificar no anotan errores de API: un
error de API ahi nunca gasto un intento, asi que cuentan 0). Un marcador con estado «error» cuyo mensaje es de API se BORRA (intentos = 0, vuelve a ser
candidato); los errores de contenido (JSON invalido, cita no verificada) y todo lo que no es un error quedan intactos. Es idempotente: una segunda corrida
no encuentra nada que resetear. Solo escribe los archivos que cambian.
"""
import re

from . import avisos_pago as avi
from . import condiciones as con
from . import eventos as eve
from . import financieros as fin

MARCAS_API = ("credit balance", "authentication", "permission", "rate limit", "ratelimit", "overloaded", "529", "429", "connection", "timeout",
              "limite de tasa sostenido", "errordecuenta", "invalid x-api-key", "billing")
_COMPLETO = "your credit balance is too low"


def es_error_de_api(mensaje: str) -> bool:
    """El mensaje guardado en el marcador (recortado a 120 caracteres) es de cuenta/API. Un BadRequestError recortado antes de su detalle
    («...'message': 'Your cred») se reconoce por el prefijo del mensaje de saldo; un 400 con otro detalle es del documento."""
    m = (mensaje or "").lower()
    if any(x in m for x in MARCAS_API):
        return True
    if m.startswith("badrequesterror"):
        i = m.find("'message': '")
        if i < 0:
            return True                                    # recortado antes del detalle: en la corrida del incidente todos eran de saldo
        resto = m[i + len("'message': '"):]
        return _COMPLETO.startswith(resto[:len(_COMPLETO)])
    return False


PASOS = (                         # (nombre, leer, guardar, clave de la lista de resultados con "doc")
    ("financieros", fin.leer_financieros, fin.guardar_financieros),
    ("avisos-pago", avi.leer_avisos, avi.guardar_avisos),
    ("condiciones", con.leer_condiciones, con.guardar_condiciones),
    ("eventos", eve.leer_eventos, eve.guardar_eventos),
)


def resetear(almacen, empresas: list) -> dict:
    """{paso: documentos reseteados} para los seis pasos con modelo."""
    cuenta = {nombre: 0 for nombre, _, _ in PASOS}
    for e in empresas:
        for nombre, leer, guardar in PASOS:
            estado = leer(almacen, e["cuit"])
            malos = [d for d, p in estado.get("procesados", {}).items() if p.get("estado") == "error" and es_error_de_api(p.get("error", ""))]
            if not malos:
                continue
            for d in malos:
                del estado["procesados"][d]
            guardar(almacen, e["cuit"], estado)
            cuenta[nombre] += len(malos)
    cuenta["resumenes"] = 0
    cuenta["clasificar"] = 0
    return cuenta


def a_texto(cuenta: dict) -> str:
    orden = ("financieros", "resumenes", "clasificar", "eventos", "condiciones", "avisos-pago")
    lineas = [f"  {p}: {cuenta[p]} documentos reseteados" for p in orden]
    return "\n".join(lineas) + f"\nTOTAL {sum(cuenta.values())} (errores de contenido: sin tocar)"
