# Calidad de las cifras de estados contables

Qué se controla, qué patrones de error se vieron y qué se corrigió. Es un documento vivo: la revisión de la muestra contra los PDF se anota abajo.

## Cómo se controla

1. **El extractor** (`cnv/financieros.py`) solo guarda una cifra si su cita está literalmente en el texto del documento y el número (con su signo) está dentro de la cita. Eso garantiza que *el número figura en el PDF*, **no** que sea el de la columna ni la fila correctas, ni que la escala esté bien declarada.
2. **La auditoría** (`python -m cnv auditar-cifras`, `cnv/auditoria.py`) cruza las cifras entre sí y entre períodos del mismo emisor; las marcadas `sospechosa` quedan fuera de los ratios.
3. **La muestra** (`python -m cnv calidad [--n 30]`, `cnv/calidad.py`) toma 30 períodos de forma determinista (hash del documento) y les aplica una revisión cruzada: el balance cuadra (1 %), los ratios caen en rangos plausibles, escala y moneda coinciden con el período anterior del mismo emisor y balance, toda cifra trae cita y página, y no hay hallazgos de la auditoría. Corre en el resumen del job `indice` y se puede correr a mano. Cuenta además los patrones de la auditoría en toda la base. **No reemplaza mirar el PDF**: lo que figure ✗ o «sospechoso» hay que validarlo contra el documento.

## Patrones de error (qué se espera y qué se midió)

Medición real: corrida `39002826223` del workflow (17/10/2026), auditoría sobre 372 períodos de 87 emisores → 69 hallazgos, 29 cifras sospechosas en 16 períodos (recuento agregado del log del job; **no tuve acceso a las cifras individuales**):

| Patrón | Hallazgos | Qué es | Estado |
|---|--:|---|---|
| `moneda_cambia` (info) | 39 | el emisor «cambió de moneda» entre períodos consecutivos | **Corregido (hipótesis)**: se comparaba el texto tal cual, y «Pesos» / «pesos argentinos» / «ARS» contaban como monedas distintas. Ahora se normaliza (`moneda_canonica`: pesos, dólares) antes de comparar, y la serie ya no se parte por la escritura de la moneda, así que también se ven saltos que antes quedaban escondidos. Falta confirmar con los textos reales cuántos de los 39 eran solo grafía y cuántos son cambios reales (p. ej. emisores que pasan a reportar en dólares). |
| `salto_entre_periodos` | 13 | una cifra cambia > 50 veces entre dos períodos consecutivos | Sin cambios. Causas esperadas: escala mal declarada por el modelo (`unidad`), columna comparativa, o crecimiento real (raro). Con 3 o más períodos se marca solo el más alejado de la mediana. |
| `escala_cambia` (info) / `unidad_incoherente` | 4 / 4 | el emisor cambia de miles a millones (o al revés) entre períodos / el salto coincide con ese cambio | Sin cambios. `unidad_incoherente` es el patrón «×1000» típico; las cifras involucradas quedan fuera de los ratios. |
| `resultado_neto_mayor_que_ventas` (+ operativo) | 4 + 1 | resultado mayor que las ventas | Sin cambios. Suele ser una fila equivocada (ventas = otra línea) o un resultado por participaciones/discontinuadas; queda fuera de ratios. |
| `balance_no_cuadra` | 2 | activo ≠ pasivo + patrimonio (1 %) | Sin cambios. Caso conocido: el patrimonio atribuible a los propietarios sin participaciones no controladoras (ver README). |
| `signo_raro` | 2 | ventas, activos, pasivos, caja o deuda negativos | Sin cambios. |

Otros motivos que ocultan ratios y conviene no confundir con errores de extracción: `falta_cifra` (1.072 casos: el extractor no encontró la fila), `periodo_no_anual` (186: no se anualiza) y `patrimonio_negativo` (24: real).

### Los tres patrones que se pedía corregir

Se pidió corregir los 3 más frecuentes. Con los datos agregados que tengo:
1. **`moneda_cambia` → corregido** (normalización de la moneda; ver arriba). Es el único cambio que el recuento respalda sin ver los PDF.
2. **`salto_entre_periodos` y `unidad_incoherente`/`escala_cambia` → no corregido.** Son los 17 hallazgos de escala y saltos; sin ver las cifras no puedo distinguir un error del modelo al declarar la unidad de un crecimiento o un cambio de presentación real, y «corregirlos» a ciegas (p. ej. reescalar por 1000 cuando el salto se parece a un múltiplo de mil) inventaría datos, que es justo lo que este proyecto evita. La muestra (`calidad`) ahora lista el detalle de escala y moneda por período para que se revise con el PDF al lado.
3. **Consolidado vs individual** no aparece en el recuento: el extractor etiqueta el balance a partir de la descripción de la CNV (`TIPO BALANCE`) y los ratios nunca mezclan balances distintos (`balances_distintos`). Si hay errores de etiquetado, se verán en la muestra como saltos entre períodos del mismo emisor.

## Revisión de la muestra contra los PDF

**Pendiente.** El entorno donde se desarrolló esto no tiene acceso al bucket ni a la CNV, así que no se revisaron 30 estados contables reales. Procedimiento: `python -m cnv calidad --salida muestra.md`, abrir el PDF de cada fila ✗/«sospechoso» (la URL está en `cnv/financieros/{cuit}.json`) y anotar acá el patrón de error y la fila corregida.

| Fecha | Empresa / doc | Qué pasó (escala, moneda, consolidado/individual, fila equivocada…) | Acción |
|---|---|---|---|
| | | | |
