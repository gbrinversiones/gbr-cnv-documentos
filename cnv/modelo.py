"""Llamada comun a Claude Haiku con salida JSON por esquema (usada por avisos_pago y clasificacion).

`cliente` es anthropic.Anthropic() o un doble en las pruebas: nunca se importa el SDK aca, asi las pruebas no tocan la red.
"""
import json

MODELO = "claude-haiku-5-5"


def pedir_json(cliente, sistema: str, entrada: str, esquema: dict, max_tokens: int = 2048) -> dict:
    """Devuelve el JSON del modelo. Lanza RuntimeError si la respuesta esta incompleta, vacia o no es un objeto JSON:
    el llamador lo trata como error reintentable (no como dato)."""
    r = cliente.messages.create(
        model=MODELO,
        max_tokens=max_tokens,
        thinking={"type": "disabled"},
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": esquema}},
        system=sistema,
        messages=[{"role": "user", "content": f"<documento>\n{entrada}\n</documento>"}],
    )
    if getattr(r, "stop_reason", None) in ("refusal", "max_tokens"):
        raise RuntimeError(f"respuesta incompleta: {r.stop_reason}")
    bloque = next((b for b in r.content if getattr(b, "type", None) == "text"), None)
    if bloque is None:
        raise RuntimeError("respuesta sin texto")
    try:
        datos = json.loads(bloque.text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"json invalido: {error}") from error
    if not isinstance(datos, dict):
        raise RuntimeError("la respuesta no es un objeto json")
    return datos
