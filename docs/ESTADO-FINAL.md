# Estado final (cierre por falta de créditos)

El workflow `sincronizar.yml` está **DESHABILITADO** hasta que el usuario lo reactive: `gh workflow enable sincronizar.yml`. No se dispara nada ni se gasta API.

## Qué hace cada paso del job `indice`
- `indice`: índice por empresa + índice global (con huella). `novedades`, `cobertura`, `series`, `perfiles`, `exportar`, `calidad`, `busqueda`, `ratios`, `salud`: derivados SIN modelo, a partir de lo ya guardado.
- `financieros`: cifras de estados contables (Haiku) con cita literal verificada; `resumenes`: resumen de hechos; `clasificar`: categoría del hecho; `eventos`, `condiciones`, `avisos-pago`: datos estructurados con cita verificada. Todo dato sin cita literal verificada se descarta con su motivo.
- Los pasos con modelo usan `CNV_CONCURRENCIA` (defecto 2) y `CNV_TPM` (defecto 150000/min). Con error de cuenta/saldo abortan sin marcar (ver README, «Se acabó el saldo»).

## Pendiente de verificar con datos reales
- Que `reintentos --reset` reconozca los mensajes reales guardados en la corrida 39065060292 (los marcadores truncan el error a 120 caracteres; un `BadRequestError` cortado antes del detalle se toma como saldo).
- Cobertura real de financieros/eventos/condiciones/avisos tras el atraso; calidad de las citas en documentos reales.
- Clave de API separada (Workspace propio) para el pipeline: acción del usuario.

## Cómo reanudar
1. Cargar saldo en la cuenta de Anthropic (idealmente con una clave/Workspace aparte del chatbot) y actualizar el secreto `ANTHROPIC_API_KEY`.
2. `gh workflow enable sincronizar.yml`.
3. Lanzar una corrida manual con `resetear_errores_api=true` (ejecuta `python -m cnv reintentos --reset` antes de los pasos con modelo) y `cupo_modelo` 2-4 para ponerse al día.
4. Orden de pasos con modelo: financieros → resumenes → clasificar → eventos → condiciones → avisos-pago; luego los derivados.
