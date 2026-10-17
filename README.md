# gbr-cnv-documentos

Descarga los documentos **públicos** que las empresas argentinas presentan en la CNV (hechos relevantes, estados contables, prospectos y suplementos), extrae su texto y lo guarda en Google Cloud Storage para que el asistente de [GBR Inversiones](https://gbr-inversiones.web.app/herramientas/asistente-renta-fija) pueda buscarlos y responder preguntas sobre cada uno.

## Cómo funciona
- `data/universo.csv`: empresas (acciones y emisores de ONs que cotizan en BYMA) con su CUIT.
- `python -m cnv sync`: lista los documentos de cada empresa, baja los que faltan, extrae el texto de los PDF (también los que vienen en ZIP) y guarda el texto y un índice por empresa. Tipos con texto: `hechos`, `estados_contables`, `prospectos` (prospectos, suplementos, avisos de suscripción y de resultado), `avisos_pago` (sección «Aviso de Pago») y `actas` (de directorio y de comisión fiscalizadora). Cualquier otra sección de la CNV queda como metadato (`t = "otros"`), sin texto.
- `python -m cnv indice`: arma el índice global que usa el buscador.
- `python -m cnv financieros`: extrae cifras clave de los estados contables ya descargados (ver abajo).
- `python -m cnv universo`: verifica los CUIT del CSV contra la CNV y sugiere candidatos para empresas que faltan (ver abajo; no modifica el CSV).
- `python -m cnv busqueda`: reconstruye el índice de búsqueda de texto completo (BM25) que lee el buscador de la landing (ver abajo).
- `python -m cnv salud`: informe de salud de la base (solo lee; ver abajo).
- `python -m cnv escaneados`: cuenta los PDF escaneados del universo según los marcadores (solo lee).
- `python -m cnv ocr`: OCR opcional de los PDF escaneados (ver abajo).
- `python -m cnv clasificar`: asigna una categoría cerrada (`k`) a cada hecho relevante (ver abajo).
- `python -m cnv novedades`: documentos nuevos entre corridas y lo presentado en los últimos 7 días, para la landing (sin modelo; ver abajo).
- `python -m cnv cobertura`: cruza emisores y ONs con documentos y datos verificados; lista los huecos (sin modelo; ver abajo).
- `python -m cnv condiciones`: condiciones de emisión de las ON desde prospectos, suplementos y avisos (ver abajo).
- `python -m cnv ratios`: ratios por emisor y periodo desde las cifras verificadas (sin modelo; ver abajo).
- `python -m cnv avisos-pago`: estructura los avisos de pago de ONs ya descargados (ver abajo).
- El workflow `Sincronizar documentos CNV` corre todos los días (y se puede disparar a mano para cargas grandes).

## Tipos de documento, topes y reintentos
- **Topes por documento** (un prospecto puede tener cientos de páginas): `--max-paginas` / `CNV_MAX_PAGINAS` (400), `--max-caracteres` / `CNV_MAX_CARACTERES` (1.500.000) y `--max-mb` / `CNV_MAX_MB` (80, tamaño del archivo descargado). Los topes se reparten entre todos los adjuntos del documento. El texto se corta en límite de página y **queda registrado**: el índice por empresa agrega `trunc` = `{"motivo": "paginas"|"caracteres", "paginas_total", "paginas_leidas"}` y el índice global agrega `tr` con el motivo (solo en los documentos truncados; el resto no cambia).
- **Descarga robusta:** cada adjunto se baja con hasta 4 intentos y espera creciente (3, 6, 9 s) ante cortes de red, 5xx o 429; los errores definitivos (404, archivo sobre el tope) no se reintentan. Cada documento tiene un tope de 15 minutos de reloj.
- **Resultados parciales:** si de dos adjuntos uno falla, se guarda el texto del que salió bien con `parcial` (cantidad de adjuntos fallidos) y `error` (motivo de cada uno). Se reintenta en corridas siguientes (hasta 3 intentos) y un reintento que traiga menos texto no pisa el que ya estaba. PDF cifrados con clave (no se pueden leer) y ZIP corruptos quedan como fallo con su motivo.
- **`fonttools`** (en `requirements.txt`): pypdf lo necesita para decodificar bien las fuentes CFF Type1 (p. ej. `Times-Roman` embebido); sin él imprime una advertencia por cada página (llenaba los logs del backfill) y puede extraer mal los caracteres de esas fuentes.
- **Un corte no pierde lo bajado:** el índice por empresa se guarda cada 10 documentos y al salir (también si el job se cancela).

### Carga histórica
Los tipos nuevos pesan más que los hechos relevantes. Para cargar el historial se dispara el workflow a mano (*Actions → Sincronizar documentos CNV → Run workflow*) o se corre local, siempre por tipo y repitiendo hasta que `pendientes` sea 0 (es idempotente: lo ya bajado no se vuelve a bajar):
```
# desde la web: desde=2023-01-01  tipos=prospectos  tiempo_max=18000   (repetir hasta que no queden pendientes)
# o local, una empresa de prueba:
DATOS_LOCAL=datos python -m cnv sync --desde 2023-01-01 --tipos prospectos --solo 30546689979 --max-paginas 400
# avisos de pago y actas son livianos: pueden ir juntos
python -m cnv sync --desde 2023-01-01 --tipos avisos_pago,actas
```
El job diario trae los últimos 30 días de los cinco tipos; no hace falta tocarlo después de la carga histórica.

## Almacenamiento
Todo cuelga de `cnv/` en el bucket. Detalle campo por campo en el [diccionario de datos](#diccionario-de-datos-bucket):
`cnv/texto/{cuit}/{doc_id}.txt.gz` · `cnv/texto/{cuit}/{doc_id}.ocr.txt.gz` · `cnv/indice/{cuit}.json` · `cnv/indice/global.json.gz` · `cnv/financieros/{cuit}.json` · `cnv/indice/financieros.json.gz` · `cnv/avisos_pago/{cuit}.json` · `cnv/indice/avisos_pago.json.gz` · `cnv/busqueda/{docs.json.gz, meta.json, s00..s63.json.gz}`

## Cifras de estados contables (`cnv/financieros.py`)
Segunda etapa: de cada documento de tipo `estados_contables` con texto, saca periodo (cierre AAAA-MM-DD), tipo (anual/trimestral/semestral), si es acumulado, moneda y unidad, y las cifras `ventas`, `resultado_operativo`, `resultado_neto`, `activo_total`, `pasivo_total`, `patrimonio_neto`, `deuda_financiera`, `efectivo` y, desde la versión 4 del extractor, `activo_corriente`, `pasivo_corriente`, `intereses` y `depreciaciones_amortizaciones` (insumos de los ratios). Los documentos procesados con la versión 3 siguen valiendo y **no se vuelven a pagar**; `python -m cnv financieros --completar-campos --max 150` los vuelve a pedir (≈ US$ 0,002 cada uno) para que sumen los campos nuevos.

- **Recorte:** no se manda el documento entero. Se puntúan las páginas por palabras clave (estado de situación financiera, estado de resultados, nota de préstamos/ONs...) y se arma un recorte de máx. 40.000 caracteres (más la portada, para el periodo).
- **Modelo:** `claude-haiku-5-5` con el SDK `anthropic`, salida JSON con esquema y `thinking` desactivado. Cada cifra viene con una cita textual de 8 a 25 palabras.
- **Regla central:** una cifra solo se conserva si (1) su cita aparece literalmente en el texto del documento (sin importar acentos, mayúsculas ni espacios) y (2) el número aparece dentro de la cita, con el mismo signo. Se interpretan separadores argentinos (`1.234.567,89`) y negativos entre paréntesis. Lo que no se puede respaldar se descarta (queda el motivo en `descartadas`); nunca se publica un número sin respaldo.
- **Escaneados:** si el PDF no tiene texto útil se marca `escaneado` y se saltea (sin llamar al modelo).
- **Idempotente:** `cnv/financieros/{cuit}.json` guarda, por documento, el id y la versión del extractor (`VERSION_EXTRACTOR`). No se reprocesa salvo que suba la versión; los errores de API se reintentan hasta 3 veces.
- **Salidas:** `cnv/financieros/{cuit}.json` (`periodos` con cifras, citas y página; `procesados` con el estado de cada documento) e `cnv/indice/financieros.json.gz` (tabla plana, un registro por empresa/periodo/tipo/acumulado; si dos documentos traen el mismo periodo queda el más nuevo). Los valores van **como figuran en el documento**; `escala` (1000 = miles, 1000000 = millones) permite pasarlos a unidades. `valor_abs` en el detalle ya está multiplicado.

```
export ANTHROPIC_API_KEY=...   # no se guarda en ningún archivo
python -m cnv financieros --max 20 --solo 30546689979 --tiempo-max 600
```
`--max` es el tope de documentos enviados al modelo por corrida (costo acotado); el workflow lo corre al final del job `indice` con `--max 150` y `continue-on-error`. Solo procesa documentos ya descargados: hay que haber corrido `python -m cnv sync --tipos estados_contables`.

## Novedades (`cnv/novedades.py`)
`python -m cnv novedades [--dias 7] [--hoy AAAA-MM-DD]` escribe `cnv/indice/novedades.json` (JSON plano, sin modelo, US$ 0) con lo **presentado** en los últimos 7 días (fecha de presentación ≥ hoy − 7, inclusive): hechos relevantes agrupados por categoría y avisos de pago. Es el último paso del job `indice`.
- **Estado entre corridas:** `cnv/indice/vistos.json` (`{"version":1,"cuits":{cuit:{doc_id: fecha en que se vio por primera vez}}}`) para hechos y avisos de pago. En la primera corrida se siembra con la fecha de presentación de cada documento, así que lo que ya estaba **no** cuenta como nuevo; después, todo documento que aparece en el índice y no estaba queda detectado ese día. Una carga histórica no llena la lista: se detecta hoy pero se presentó hace años, y la ventana mira la fecha de presentación.
- **Formato de `novedades.json`:** `{"version":1,"al","desde","dias","resumen":{hechos, hechos_por_categoria, avisos_pago, pagos_estructurados, nuevos_en_esta_corrida},"hechos_por_categoria":{categoria:[{cuit, empresa, doc, fecha, titulo, resumen, url, categoria, detectado, texto_ok}]},"avisos_pago":[{cuit, empresa, doc, fecha_presentacion, estructurado, fecha_pago, tipo_pago, clase, ticker, monto_total, moneda, url, detectado}]}`. Categorías: las de la lista cerrada de la clasificación más `sin_clasificar` (sin `k` todavía o categoría fuera de la lista). Orden: más nuevo primero. Un hecho cuyo texto no se pudo bajar igual aparece (con el título de la CNV; `texto_ok: false`).
- **Avisos de pago:** una fila por pago estructurado (con los datos verificados de `cnv/avisos_pago`); si el aviso todavía no se estructuró, una fila con `estructurado: false`. `detectado` permite a la landing resaltar lo de hoy (presentado hace 3 días y detectado hoy es una novedad de hoy). `nuevos_en_esta_corrida` cuenta lo detectado en esa ejecución.
- No se vuelve a escribir ningún archivo si el contenido es igual (`al` cambia cada día, así que `novedades.json` sí se actualiza diariamente).
## Cobertura por emisor y ON (`cnv/cobertura.py`)
`python -m cnv cobertura [--ons data/ons.csv] [--hoy AAAA-MM-DD]` cruza `data/universo.csv` y `data/ons.csv` con lo que ya hay en el bucket (sin modelo, sin red, US$ 0) y escribe `cnv/indice/cobertura.json.gz`. Corre en el job `indice`, después de `avisos-pago` y antes de `salud`.
- **`data/ons.csv`** (`ticker, emisor, descripcion, vence, cuit, fuente_cuit`): una fila por ON de renta fija, con el CUIT de su emisor. Se armó a partir del catálogo de ONs de `portfolio-manager` (`data/ons_catalogo.csv` y `prospectos_por_on.csv`; solo datos públicos: ticker, descripción y vencimiento, sin precios). El CUIT sale de una coincidencia **exacta** de nombre con `universo.csv` (o con `cnv_cuits_emisores.csv`), y las 7 filas restantes se resolvieron a mano donde coincidían las dos fuentes (`fuente_cuit` lo dice). No hay coincidencia aproximada en tiempo de ejecución. Para sumar una ON, agregar la fila con su CUIT; un test exige que todos los CUIT sean válidos y estén en el universo.
- **Formato:** `{"version":1,"al":"AAAA-MM-DD","huecos_definicion":{...},"resumen":{...},"emisores":[...],"ons_sin_emisor":[...]}`. Cada emisor (todas las empresas del universo, por nombre): `cuit`, `empresa`, `ons` (`ticker`, `vence`, `ultimo_pago` = última fecha de pago estructurada para ese ticker), `documentos` (por tipo: `total`, `con_texto`, `ultimo`), `ultimo_estado_contable` (`doc`, `periodo`, `tipo`, `balance`, `fecha_presentacion`, `cifras`; solo con cifras verificadas), `ultimo_aviso_pago` (el último estructurado, o el último documento con `estructurado: false`) y `huecos`.
- **Huecos** (avisos accionables, no errores): `sin_documentos`, `sin_estados_contables`, `estados_contables_sin_cifras`, `estado_contable_desactualizado` (cierre de hace más de 200 días), `sin_prospectos` y `sin_avisos_de_pago` (solo si el emisor tiene ONs), `avisos_de_pago_sin_estructurar`, `documentos_fallidos`, `documentos_sin_bajar`, `sin_actividad_reciente` (más de 120 días). `ons_sin_emisor`: ONs cuyo CUIT falta (`sin_cuit_en_ons_csv`) o cuyo emisor no está en el universo (`emisor_fuera_del_universo`, o sea que no se están bajando sus documentos).
- Depende de la fecha (`al`) para los huecos por antigüedad, así que el archivo puede cambiar de un día para otro aunque no haya documentos nuevos; no se vuelve a escribir si el contenido es igual.

## Condiciones de emisión de las ON (`cnv/condiciones.py`)
De cada documento de tipo `prospectos` con texto (prospecto, suplemento, aviso de suscripción o de resultado; `tipo_documento` sale de la sección de la CNV) saca, por clase/serie, con `claude-haiku-5-5` (esquema JSON, `thinking` apagado) y la **misma regla de cita literal verificada** que `financieros`:
`clase`, `moneda` (`pesos`/`dolares`/`uva`/`dolar_linked`), `monto_maximo`, `monto_adjudicado`, `tasa_tipo` (`fija`/`variable`/`mixta`/`cupon_cero`), `tasa_valor`, `tasa_margen`, `tasa_referencia`, `fecha_emision`, `fecha_vencimiento`, `frecuencia_intereses` (`mensual`/`trimestral`/`semestral`/`anual`/`al_vencimiento`), `fechas_pago_intereses`, `amortizacion_tipo` (`bullet`/`cuotas`), `amortizacion_fechas`, `rescate_anticipado`, `garantia`, `ley_aplicable` y `calificacion`.
- **Verdad:** un dato se publica solo si su cita (3–50 palabras) está literal en el texto del documento y el dato está respaldado por la cita: **números** (montos, tasas) tal cual impresos y enteros dentro de la cita (con `%` en las tasas); **fechas** en alguno de sus formatos dentro de la cita; **categorías** con palabras de la cita que las justifican (p. ej. «nominal anual» **no** respalda una frecuencia anual); **textos** como fragmento copiado literal de la cita. Una emisión sin ningún dato clave (vencimiento, emisión, tasa o monto) no se publica. Lo descartado queda con su motivo en `procesados[doc].descartados`. Un documento leído por OCR no se usa.
- **Recorte y costo:** se manda la portada más las páginas con más palabras clave (términos y condiciones, tasa, vencimiento…) hasta 40.000 caracteres; ≈ US$ 0,002 por documento (estimado). `--max 100` por corrida (workflow: job `indice`, `continue-on-error`). Idempotente (versión del extractor, 3 reintentos de error, guardado tras cada documento).
- **Salidas:** `cnv/condiciones/{cuit}.json` (`emisiones[]` con `doc`, `n`, `tipo_documento`, `campos{campo: {valor, cita, pagina, (texto, ambiguo, moneda_en_cita)}}`; y `procesados`) y `cnv/indice/condiciones.json.gz` = `{"version":1,"filas":[...],"ons":{ticker:{...}}}`: una fila plana por emisión (sin citas; con `ticker` si calza con exactamente una ON y `tickers` si calza con varias) y, por ticker de `data/ons.csv`, las condiciones **consolidadas**.
- **Vínculo ticker → condiciones:** solo por CUIT del emisor + **fecha de vencimiento verificada** igual a la de la ON; sin fecha respaldada en el documento no hay vínculo (no se adivina por nombre ni por clase). Si hay varios documentos de la misma emisión, por campo manda el suplemento (el monto adjudicado, el aviso de resultado); `fuentes` dice de qué documento y página salió cada valor y `conflictos` lista los campos en que otro documento dice algo distinto.
- **Diferencia con `rf_terminos.json`** (repo `portfolio-manager`, `newsletter/terminos_on.py`): aquel sale solo del **Aviso de Resultados**, deja un texto libre resumido por ticker (`tasa_interes`, `pago_intereses`, `amortizacion`…), se asocia a la ON por emisor y fecha de emisión y se publica en el repo de assets. Este sale del **prospecto, suplemento y avisos de suscripción/resultado**, con campos **tipados** (números, fechas ISO, categorías) y citas por campo, y queda en la base pública de la CNV. No se reemplazan: `rf_terminos.json` sigue sirviendo mientras la base no cubra una ON; el campo `fuentes` permite contrastar ambos.
```
export ANTHROPIC_API_KEY=...
python -m cnv condiciones --max 20 --solo 30546689979     # gasta modelo
python -m cnv condiciones --solo-indice                   # solo reconstruye el índice (US$ 0)
```

## Ratios por emisor (`cnv/ratios.py`)
`python -m cnv ratios` calcula, por emisor y periodo, a partir de las cifras **ya verificadas** de `cnv/financieros/{cuit}.json` (no usa el modelo, cuesta US$ 0) y escribe `cnv/indice/ratios.json.gz`.
| Ratio | Fórmula | Condiciones |
|---|---|---|
| `liquidez_corriente` | activo_corriente / pasivo_corriente | pasivo corriente > 0 |
| `endeudamiento` | pasivo_total / patrimonio_neto | patrimonio > 0 (con patrimonio negativo o cero queda `null`: `patrimonio_negativo` / `division_por_cero`) |
| `deuda_ebitda` | deuda_financiera / (resultado_operativo + \|depreciaciones_amortizaciones\|) | solo periodos **anuales**; EBITDA > 0 |
| `margen_neto` | resultado_neto / ventas | ventas > 0 |
| `roe` | resultado_neto / patrimonio_neto (al cierre, no promedio) | solo periodos **anuales**; patrimonio > 0 |
| `cobertura_intereses` | resultado_operativo / \|intereses\| | intereses ≠ 0 |
- **Reglas:** todas las cifras de un ratio deben ser del mismo periodo (cierre, tipo y acumulado), la misma moneda, la misma unidad/escala y el mismo tipo de balance (consolidado/individual); si falta una cifra o algo no cierra, el ratio es `null` y `motivos[ratio]` dice por qué (`falta_cifra:<campos>`, `periodos_distintos`, `monedas_distintas`, `unidades_distintas`, `moneda_desconocida`, `division_por_cero`, `patrimonio_negativo`, `periodo_no_anual`, …). Nunca se estima, no se anualizan trimestres ni semestres (por eso `roe` y `deuda_ebitda` quedan `null` con `periodo_no_anual` en periodos parciales) y no se completa una cifra con la de otro documento. Un estado contable procesado antes de la versión 4 no tiene las cifras nuevas: su liquidez, deuda/EBITDA y cobertura quedan `null` (`falta_cifra`) hasta completarlo con `--completar-campos`.
- **Trazabilidad:** cada periodo trae `doc` (el documento de origen en `cnv/financieros`), `insumos` (`valor` y `pagina` de cada cifra), `derivados` (`ebitda`) y `periodo_verificado` / `balance_coherente` tal como figuran en financieros. Si dos documentos traen el mismo periodo queda el más nuevo; consolidado e individual van por separado.
- **Formato:** `{"version":1,"formulas":{...},"emisores":{cuit:{"empresa","periodos":[{periodo,tipo,acumulado,balance,moneda,unidad,escala,doc,periodo_verificado,balance_coherente,insumos,derivados,ratios,motivos}]}}}`, ordenado por cierre descendente, gzip determinista. El workflow lo corre en el job `indice` justo después de `financieros`.

## Avisos de pago de ONs (`cnv/avisos_pago.py`)
Del documento de tipo `avisos_pago` saca, por cada pago: `emisor`, `clase`, `ticker` (si figura), `tipo_pago` (`interes` | `amortizacion` | `ambos`), `fecha_pago`, `monto_total`, `moneda` (`pesos` | `dolares`), `valor_residual` y `tasa`. Es el dato más confiable para saber qué se pagó realmente.
- **Verdad:** cada dato viene de `claude-haiku-5-5` (esquema JSON) con una cita de 2 a 45 palabras, y solo se publica si la cita está **literal** en el texto y el dato está en la cita: el número tal cual impreso (sin confundir `1.875` con `1.875.000`), la fecha en alguno de sus formatos, el texto del emisor/clase/ticker (ticker `[A-Z0-9]{3,8}`), las palabras *interés*/*amortización* para el tipo de pago y el símbolo (`US$`, `$`, «pesos»…) para la moneda. Un pago sin fecha o sin tipo respaldados no se publica. Lo descartado queda en `procesados[doc].descartados` con el motivo.
- **Formularios sin valores:** el PDF del «Aviso de Pago» de la CNV suele ser un formulario cuyo texto extraíble son solo los rótulos (los valores están dibujados como vectores). Se detecta (nombra obligaciones negociables y tiene < 20 dígitos), queda como `formulario_sin_valores` y **no se publica ningún pago** ni se llama al modelo. Con OCR activado (`python -m cnv ocr`, job `ocr`) se renderiza el PDF a 300 dpi y se lee con tesseract descartando los números de baja confianza (`CNV_OCR_CONFIANZA`, 70: se reemplazan por `[ilegible]`); el texto va a la ruta nueva `cnv/texto/{cuit}/{doc_id}.ocr.txt.gz` (el texto original no se toca) y el aviso se reevalúa contra ESE texto (`fuente: "ocr"`). Una cita con `[ilegible]` se descarta; si no queda una fecha y un tipo respaldados, el estado es `sin_valores_legibles` y no se publica nada. `python -m cnv avisos-pago --contar-formularios` (solo lee) cuenta cuántos avisos caen en este caso.
- **Serie de la CNV:** la página de la presentación declara la serie (`DescripcionDeLaSerieIndividual`, `IDSerieIndividual`, `Observaciones`); se guarda en `serie` del índice por empresa y sale en la tabla plana como `serie_cnv`, **separada** de `clase` (que sigue exigiendo cita en el texto).
- **Ahorro:** textos cortos (< 200 caracteres) y avisos que no nombran obligaciones/títulos de deuda (p. ej. dividendos) se saltean sin llamar al modelo.
- **Idempotente:** igual que `financieros` (versión del extractor + hasta 3 reintentos de error; se guarda tras cada documento).
- **Salidas:** `cnv/avisos_pago/{cuit}.json` (pagos con cita y página) y `cnv/indice/avisos_pago.json.gz` (una fila por pago, sin citas; si dos avisos traen el mismo pago, queda el más nuevo). Los números van como figuran; `valor_residual_unidad`/`tasa_unidad` dicen si son `porcentaje` o `monto`, y `ambiguo` lista los números que podrían leerse en formato inglés (`1,500`).
```
export ANTHROPIC_API_KEY=...
python -m cnv avisos-pago --max 50 --solo 30546689979
```

## Categoría de los hechos relevantes (`cnv/clasificacion.py`)
Cada hecho relevante con texto recibe `k` en el índice por empresa y en `cnv/indice/global.json.gz` (`null` si todavía no está clasificado o no es un hecho). Lista cerrada: `resultados`, `dividendos`, `recompra_acciones`, `recompra_on`, `emision_on`, `cambio_directorio`, `adquisicion_venta`, `hecho_judicial`, `convocatoria_asamblea`, `calificacion_riesgo`, `otro`.
- **Modelo:** `claude-haiku-5-5`, esquema JSON con `enum` cerrado y una cita de 3 a 40 palabras. La cita se compara ignorando comillas, guiones, puntuación, espacios y saltos de línea (solo letras y números). Si igual no figura, se hace **un** reintento pidiendo un fragmento más corto copiado literal; recién ahí se descarta. Una categoría fuera de la lista, o (salvo `otro`) sin cita verificable, **se descarta**: el hecho queda sin `k` y se cuenta el intento en `kx` (máx. 3).
- **Idempotente:** `kv` guarda la versión del clasificador; solo se reclasifica si cambia. Los errores de red/API no gastan intento.
- Los campos nuevos (`k`, `kv`, `kx`) solo se agregan; `r` (resumen) y el resto no cambian.
```
export ANTHROPIC_API_KEY=...
python -m cnv clasificar --max 100
```

## PDF escaneados y OCR opcional (`cnv/ocr.py`)
- **Marcador:** un documento cuyo texto extraído tiene menos de 25 caracteres por página (solo las marcas `[pagina N]`) queda con `escaneado: true` en el índice por empresa. Se guarda igual que antes. (Antes de esta versión ese marcador casi nunca se activaba; los documentos viejos no lo tienen: para esos vale `procesados[*].estado == "escaneado"` de `financieros`.) `python -m cnv escaneados` cuenta ambos marcadores por tipo y empresa.
- **OCR (apagado por defecto):** `CNV_OCR=1` activa `pytesseract` + `pdf2image`; el sync normal, si la variable está puesta, lee por OCR los PDF escaneados al bajarlos, y `python -m cnv ocr` reprocesa los ya marcados (usa la URL guardada, no vuelve a consultar el listado de la CNV). `CNV_OCR_MAX_PAGINAS` (40), `CNV_OCR_IDIOMA` (`spa`), `CNV_OCR_DPI` (200). Si falta tesseract, el idioma o poppler no se toca nada y el documento no queda como «intentado».
- **Marca de calidad:** lo leído por OCR queda con `ocr` = páginas reconocidas (índice por empresa) y `oc` en el índice global; si se cortó por el tope, `trunc.motivo = "ocr_paginas"`. **`financieros` y `avisos-pago` no usan documentos con OCR**: un dígito mal leído igual «verificaría» contra su propia cita.
- **Workflow:** job `ocr` aparte (instala `tesseract-ocr`, `tesseract-ocr-spa`, `poppler-utils` y `requirements-ocr.txt`), con `continue-on-error`; el job `indice` corre después aunque falle.
```
sudo apt-get install -y tesseract-ocr tesseract-ocr-spa poppler-utils && pip install -r requirements-ocr.txt
python -m cnv escaneados                      # cuantos hay
CNV_OCR=1 python -m cnv ocr --solo 30546689979 --max-docs 3
```

## Salud de la base (`cnv/salud.py`)
`python -m cnv salud [--formato md|json] [--dias-silencio 60] [--hoy AAAA-MM-DD] [--max-fallidos 30]` lee los índices guardados (sin red ni modelo) y emite, en Markdown (defecto) o JSON:
- por empresa: documentos por tipo (total / con texto), fecha del último documento, **fallidos con su motivo** (`rendido` = agotó 3 intentos), documentos sin bajar, escaneados, truncados, parciales y leídos por OCR;
- **cobertura** de resúmenes, categorías, cifras de estados contables (con el estado de cada documento: `ok`, `escaneado`, `pendiente`…) y avisos de pago estructurados;
- **empresas silenciosas:** activas (tuvieron algún documento en los últimos 365 días) pero sin documentos nuevos hace más de `--dias-silencio` días; empresas sin ningún documento; filas del universo sin CUIT o con CUIT que no pasa el dígito verificador.
El workflow lo escribe en el resumen del job (`python -m cnv salud >> "$GITHUB_STEP_SUMMARY"`), con `continue-on-error`. «Último documento» incluye todas las secciones que lista la CNV, no solo las que se descargan.

## Control del universo (`cnv/universo.py`)
**No modifica `data/universo.csv`**: genera un informe y las altas/correcciones se hacen a mano.
```
python -m cnv universo --verificar --salida informes/universo.json     # confirma cada CUIT contra la CNV
python -m cnv universo --sugerir "Transportadora de Gas del Sur" --sugerir "Oiltanking" --sugerir "Puentes" --sugerir "Vaca Muerta Oil Sur" --sugerir "C&C Medicals"
python -m cnv universo --sugerir "Edesa" --confirmar                   # abre la página de cada candidato
```
- **`--verificar`:** abre la página pública de cada empresa (`Empresas/Empresa/{cuit}?fdesde=01/01/2016`; con una ventana reciente y sin presentaciones la CNV muestra «No se encontrarón resultados» en vez de la razón social) y compara la razón social del marcador «Resultados de búsqueda:» con la del CSV (sin acentos, mayúsculas, sufijos societarios ni alias entre paréntesis; `Cia.`=Compañía, `Hnos.`=Hermanos, `Imp.`/`Exp.`=Importadora/Exportadora). Veredictos: `coincide`, `revisar_nombre_parcial`, `razon_social_distinta`, `cuit_no_existe_en_cnv` (la CNV muestra la portada genérica), `sin_razon_social_legible`, `cuit_invalido` (dígito verificador; no gasta consulta), `sin_cuit`, `error_de_red`; más CUIT y nombres duplicados. Reintentos con espera creciente, tope de tiempo y avance en `--salida` (reanudable; los `error_de_red` se reintentan).
- **`--sugerir NOMBRE`:** consulta el autocompletado público (`Empresas/Autocomplete?term=`) con el nombre completo y versiones más cortas, lista los candidatos con CUIT, parecido, validez del CUIT y si ya están en el CSV.
- Se puede correr desde GitHub (*Actions → Verificar universo CNV*): el informe queda en el resumen del job y como artefacto.

## Índice de búsqueda de texto completo (`cnv/busqueda.py`)
Para encontrar documentos por lo que dicen y no solo por el título. `python -m cnv busqueda [--tiempo-max 1500]` reconstruye **todo** (idempotente: mismos bytes si el corpus no cambió, y no vuelve a subir lo que no cambió). Lee el texto de los documentos con `texto_ok` de tipo `hechos`, `prospectos`, `avisos_pago` y `actas` (no estados contables), solo los primeros 20.000 caracteres (sin las marcas `[pagina N]`).
- **Tokenización** (idéntica en el lector JS): NFD y sacar marcas combinantes → minúsculas → `/[a-z0-9]+/` → descartar largo < 3 o > 30 → descartar stopwords (`STOPWORDS`, lista fija) → sin stemming. La consulta se tokeniza igual.
- **Archivos:** `cnv/busqueda/docs.json.gz` = `[[cuit, doc_id, longitud_en_tokens], ...]` (el orden es el `doc_idx`: por cuit y por id numérico); `cnv/busqueda/meta.json` = `{"version":1,"n_docs","avgdl","shards":64,"generado"}`; `cnv/busqueda/s00.json.gz … s63.json.gz` = `{"termino": [[doc_idx, tf], ...]}` con el shard de un término = `zlib.crc32(termino.encode("utf-8")) % 64`. Solo entran términos con `df >= 2` y `df <= 40%` de los documentos; `tf` acotado a 255; postings ordenados por `doc_idx`. Los 64 shards existen siempre (vacíos si hace falta).
- **Publicación:** primero los shards, luego `docs.json.gz`, al final `meta.json` (el lector arranca por `meta.json`). Si se acaba el tiempo antes de publicar, **no se escribe nada** y el índice anterior sigue vigente (exit code 3).
- **Oráculo:** `buscar_bm25(almacen, consulta, k)` (BM25, `k1=1.2`, `b=0.75`, `idf = ln(1 + (N-df+0.5)/(df+0.5))`, desempate por menor `doc_idx`) y `tests/fixtures/busqueda_oraculo.json` (corpus sintético, índice resultante, stopwords, casos de tokenización, shards literales y 3 consultas con su ranking) para el test cruzado con el lector. Regenerar con `python -m tests.generar_oraculo` solo si cambia el contrato (y subir `VERSION`).
- **Tamaño estimado para 20.000 documentos:** ~11 MB en total (64 shards de ~150–190 KB gz + `docs.json.gz` de ~90 KB); una consulta baja un shard por término. Es una simulación con un corpus sintético tipo Zipf (6,6 M de tokens, 145 mil términos), no una medición con documentos reales.

## Diccionario de datos (bucket)
Regla general: **solo se agregan campos**; ninguna ruta ni campo existente cambia de significado. Un campo opcional que no figura significa «no aplica / no pasó». Las fechas son `AAAA-MM-DD`. Los «números tal cual figuran» no están convertidos a unidades (ver `escala`).

### `cnv/texto/{cuit}/{doc_id}.txt.gz`
Texto UTF-8 comprimido del documento. Cada página es un bloque `[pagina N]\n<texto>`; los bloques se separan con una línea en blanco. Si la presentación trae varios adjuntos (hasta 3) o un ZIP con varios PDF, se concatenan y **la numeración de páginas reinicia en 1 en cada PDF**. Respeta los topes de páginas/caracteres (ver `trunc`). Un PDF escaneado queda con solo las marcas (`escaneado` en el índice).

### `cnv/texto/{cuit}/{doc_id}.ocr.txt.gz`
Solo para avisos de pago que son un formulario sin valores en el texto: la lectura por OCR (300 dpi) con los números de baja confianza reemplazados por `[ilegible]`. El texto original no se toca.

### `cnv/indice/{cuit}.json` — lista de documentos de la empresa
| Campo | Significado |
|---|---|
| `id` | número de la presentación en la CNV (string) |
| `c`, `e` | CUIT (11 dígitos) y razón social del universo |
| `f` | fecha de presentación |
| `t` | tipo: `hechos`, `estados_contables`, `prospectos`, `avisos_pago`, `actas`, `otros` (solo metadato) |
| `s` | sección de la CNV tal cual |
| `d` | descripción (≤ 300 caracteres) |
| `u` | URL pública de la presentación |
| `texto_ok` | `true` si hay texto guardado |
| `intentos`, `error` | intentos de descarga (se rinde a los 3) y último motivo de fallo |
| `p`, `n` | páginas totales del PDF y caracteres del texto guardado |
| `archivos` | nombres de los adjuntos leídos (≤ 3) |
| `trunc` | `{motivo: "paginas"\|"caracteres"\|"ocr_paginas", paginas_total, paginas_leidas}` si el texto quedó cortado |
| `parcial`, `error` | cantidad de adjuntos que fallaron aunque otro salió bien |
| `escaneado` | el texto tiene < 25 caracteres por página (es una imagen) |
| `ocr`, `ocr_intentado` | páginas leídas por OCR; o el OCR corrió y no sacó texto |
| `ocr_form`, `ocr_form_numeros`, `ocr_form_ilegibles`, `ocr_form_intentado` | OCR de formularios de aviso de pago (páginas, números confiables, números ilegibles, intentado sin resultado) |
| `serie` | `{d, id, obs}`: serie que declara la CNV en la presentación (si la pudo leer) |
| `r` | resumen de una línea (solo `hechos`) |
| `k`, `kv`, `kx` | categoría del hecho (lista cerrada), versión del clasificador e intentos de clasificación descartados |

### `cnv/indice/global.json.gz` — lo que busca el explorador
Una fila por documento **con texto**: `id, c, e, f, t, s, d, u, p, n, r, k` (los que no aplican van `null`), más `tr` (motivo del truncado) y `oc` (páginas por OCR) solo cuando existen. Orden: fecha descendente.

### `cnv/financieros/{cuit}.json`
`{"cuit", "periodos": [...], "procesados": {doc_id: {...}}}`.
- `periodos[]`: `doc`, `empresa`, `fecha_presentacion`, `url`, `balance` (`consolidado`/`individual`/`desconocido`), `periodo` (cierre), `tipo` (`anual`/`trimestral`/`semestral`), `acumulado`, `moneda`, `unidad`, `escala` (1, 1000, 1000000), `periodo_verificado`, `unidad_verificada`, `balance_coherente` (activo ≈ pasivo + patrimonio, o `null`), `cifras` y `descartadas`.
- `cifras{campo}` con `campo` ∈ `ventas, resultado_operativo, resultado_neto, activo_total, pasivo_total, patrimonio_neto, deuda_financiera, efectivo, activo_corriente, pasivo_corriente, intereses, depreciaciones_amortizaciones` (los 4 últimos desde la versión 4): `{valor (tal cual figura), valor_abs (× escala), cita (literal), pagina}`. `descartadas[]`: `{campo, motivo}`.
- `procesados{doc_id}`: `v` (versión del extractor), `estado` (`ok`, `escaneado`, `sin_paginas_relevantes`, `no_es_estado_contable`, `sin_periodo`, `sin_cifras`, `otra_entidad`, `error`), `fecha`, y `intentos`/`error` si falló.

### `cnv/indice/financieros.json.gz`
Una fila por empresa/periodo/tipo/acumulado/balance: `cuit, empresa, periodo, tipo, acumulado, balance, moneda, unidad, escala, doc, fecha, periodo_verificado, balance_coherente` + los 12 campos de cifras (valor tal cual figura o `null`).

### `cnv/indice/novedades.json` y `cnv/indice/vistos.json`
Ver «Novedades». `vistos.json` es estado interno entre corridas (no lo lee la landing).
### `cnv/condiciones/{cuit}.json` y `cnv/indice/condiciones.json.gz`
Ver «Condiciones de emisión de las ON».

### `cnv/indice/cobertura.json.gz`
Ver «Cobertura por emisor y ON».

### `cnv/indice/ratios.json.gz`
Ver «Ratios por emisor». Un objeto por cuit con la lista de periodos; cada ratio es un número o `null` con su motivo.

### `cnv/avisos_pago/{cuit}.json`
`{"cuit", "pagos": [...], "procesados": {...}}`.
- `pagos[]`: `doc`, `empresa`, `fecha_presentacion`, `url`, `emisor {valor, cita, pagina}` (o `null`), `fuente` (`pdf`/`ocr`), `serie_cnv` (metadata de la CNV, **no** verificada con cita) y los campos `clase`, `ticker`, `tipo_pago` (`interes`/`amortizacion`/`ambos`), `fecha_pago`, `monto_total`, `moneda` (`pesos`/`dolares`), `valor_residual`, `tasa`; cada uno `{valor, cita, pagina}` y, en los numéricos, `texto` (el número como figura), `ambiguo` (si podría leerse `1,500` como 1,5 o 1500) y `unidad` (`porcentaje`/`monto`, en valor residual y tasa). Todo campo presente tiene cita literal en el texto.
- `procesados{doc_id}`: `v`, `estado` (`ok`, `sin_texto`, `no_es_pago_on`, `formulario_sin_valores`, `no_es_aviso_pago_on`, `sin_datos`, `sin_valores_legibles`, `error`), `fecha`, `descartados[] {pago, campo, motivo}`.

### `cnv/indice/avisos_pago.json.gz`
Una fila por pago (sin citas): `cuit, empresa, doc, fecha` (presentación), `emisor, fuente, serie_cnv, clase, ticker, tipo_pago, fecha_pago, monto_total, moneda, valor_residual, tasa, valor_residual_unidad, tasa_unidad, ambiguo`. Si dos avisos traen el mismo pago (rectificatoria) queda el más nuevo.

### `cnv/busqueda/*`
Índice BM25 de texto completo; formato y contrato con el lector en la sección «Índice de búsqueda de texto completo».

### Fuera del bucket
`python -m cnv universo --salida informes/universo.json` y `python -m cnv salud --formato json` producen informes locales (no se guardan en el bucket).

## Costos estimados con Haiku
Modelo: `claude-haiku-5-5`, **US$ 0,10 por millón de tokens de entrada y US$ 0,50 por millón de salida** (prompts de hasta 100.000 tokens; precios de la lista de la API a 2026-10-06, a verificar). No se usa caché de prompts. Los tokens de abajo son **estimaciones a partir de la cantidad de caracteres** (≈ 3,5 caracteres por token en prosa y ≈ 2,8 en balances llenos de números); el costo real se mide con `response.usage` (no está instrumentado todavía). No pude medir nada con documentos reales.

| Etapa (comando) | Documento | Entrada típica | Salida típica | Costo por documento | Por 1.000 documentos |
|---|---|---|---|---|---|
| Descarga de `prospectos`, `actas`, `hechos`, `estados_contables`, `avisos_pago` | todos | — | — | **US$ 0** (sin modelo) | 0 |
| Resumen (`resumenes`) | hecho relevante | ≈ 1.400 tokens (hasta 4.000 caracteres + instrucciones) | ≈ 70 (máx. 300) | ≈ US$ 0,00018 | ≈ US$ 0,18 |
| Categoría (`clasificar`) | hecho relevante | ≈ 2.200 (hasta 6.000 caracteres + instrucciones) | ≈ 40; un 2.º pedido si la cita no figura | ≈ US$ 0,00024 (≈ 0,0003 con reintentos) | ≈ US$ 0,25–0,35 |
| Cifras (`financieros`) | estado contable | ≈ 16.000 (recorte de hasta 43.000 caracteres) | ≈ 800 (máx. 4.096) | ≈ US$ 0,002 (máx. ≈ 0,004) | ≈ US$ 2–4 |
| Condiciones de emisión (`condiciones`) | prospecto / suplemento / aviso | ≈ 12.000 (recorte de hasta 40.000 caracteres) | ≈ 1.200 (máx. 6.000) | ≈ US$ 0,002 (máx. ≈ 0,005) | ≈ US$ 2–5 |
| Avisos de pago (`avisos-pago`) | aviso con datos | ≈ 2.000 (hasta 20.000 caracteres ≈ 7.000) | ≈ 350 | ≈ US$ 0,0004 (máx. ≈ 0,001) | ≈ US$ 0,4–1 |
| Avisos de pago, formulario | formulario sin valores | — | — | **US$ 0** el aviso sin OCR; el OCR es CPU, no modelo | 0 |
| Búsqueda (`busqueda`), OCR, salud, universo | — | — | — | **US$ 0** (sin modelo) | 0 |

**Tope diario por diseño** (los `--max` del workflow): 150 estados contables + 400 resúmenes + 500 categorías + 200 avisos ⇒ **≈ US$ 0,6 por día con los costos típicos y ≈ US$ 1 en el peor caso** (0,6 + 0,07 + 0,15 + 0,2). Una carga histórica de, por ejemplo, 10.000 hechos (resumen + categoría ≈ US$ 4,8) + 3.000 estados contables (US$ 6–12) + 3.000 avisos (US$ 1,2–3) cuesta **del orden de US$ 12–20** (depende de los volúmenes reales).

## Tiempos de carga
**Mediciones reales: ninguna** (no tengo acceso a la CNV ni al bucket). Lo que sigue son topes de configuración y un modelo simple para dimensionar.
- **Descarga (`sync`)**: la CNV manda. Cada documento son ≥ 3 pedidos (página, llave, archivo) con hasta 4 reintentos por adjunto y tope de 15 minutos por documento. El workflow corre 4 fragmentos en paralelo × 3 hilos = 12 documentos a la vez, con un tope de 5 horas por fragmento. Capacidad por corrida = 12 × 18.000 s ÷ segundos por documento: **si cada documento tarda 10 s, ≈ 21.000 por corrida; a 20 s, ≈ 10.800; a 60 s, ≈ 3.600**. Lo que no entra queda pendiente y se retoma en la corrida siguiente (idempotente).
- **Etapas con modelo** (por corrida, topes del workflow): `financieros` ≤ 150 documentos y 20 min; `resumenes` ≤ 400 y 25 min; `clasificar` ≤ 500 y 15 min; `avisos-pago` ≤ 200 y 15 min. Una carga histórica grande se hace repitiendo el workflow manual hasta que `pendientes` sea 0 (o subiendo `--max`).
- **Índice de búsqueda**: ≤ 25 min; en una simulación local con 20.000 documentos sintéticos tardó ≈ 40 s con disco local (con GCS domina la lectura de los textos).
- **OCR**: job aparte de hasta 150 min; el tope práctico es `CNV_OCR_MAX_PAGINAS` (40) por documento.
- **Salud / universo / escaneados**: segundos a minutos; `universo --verificar` hace una consulta por empresa (≈ 121) con pausa de 0,5 s.

## Configuración (repo)
- Secret `GCP_SA_KEY`: JSON de la cuenta de servicio con rol *Storage Object Admin* sobre el bucket.
- Variable `GCS_BUCKET`: nombre del bucket.
- Secret `ANTHROPIC_API_KEY`: solo para `financieros`, `resumenes`, `clasificar` y `avisos-pago`.

## Desarrollo
```
pip install -r requirements.txt
python -m pytest -q --cov=cnv --cov-fail-under=90     # ~400 pruebas, ~99% de cobertura; sin red ni claves
DATOS_LOCAL=datos python -m cnv sync --desde 2026-09-01 --solo 30546689979 --max-docs 5
```

Sin `GCS_BUCKET` guarda en la carpeta local `datos/`. La información es pública; este proyecto solo la ordena. No es asesoramiento financiero.
