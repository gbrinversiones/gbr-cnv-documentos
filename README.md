# gbr-cnv-documentos

Descarga los documentos **públicos** que las empresas argentinas presentan en la CNV (hechos relevantes, estados contables, prospectos y suplementos), extrae su texto y lo guarda en Google Cloud Storage para que el asistente de [GBR Inversiones](https://gbr-inversiones.web.app/herramientas/asistente-renta-fija) pueda buscarlos y responder preguntas sobre cada uno.

## Cómo funciona
- `data/universo.csv`: empresas (acciones y emisores de ONs que cotizan en BYMA) con su CUIT.
- `python -m cnv sync`: lista los documentos de cada empresa, baja los que faltan, extrae el texto de los PDF (también los que vienen en ZIP) y guarda el texto y un índice por empresa. Tipos con texto: `hechos`, `estados_contables`, `prospectos` (prospectos, suplementos, avisos de suscripción y de resultado), `avisos_pago` (sección «Aviso de Pago») y `actas` (de directorio y de comisión fiscalizadora). Cualquier otra sección de la CNV queda como metadato (`t = "otros"`), sin texto.
- `python -m cnv indice`: arma el índice global que usa el buscador.
- `python -m cnv financieros`: extrae cifras clave de los estados contables ya descargados (ver abajo).
- `python -m cnv universo`: verifica los CUIT del CSV contra la CNV y sugiere candidatos para empresas que faltan (ver abajo; no modifica el CSV).
- `python -m cnv salud`: informe de salud de la base (solo lee; ver abajo).
- `python -m cnv escaneados`: cuenta los PDF escaneados del universo según los marcadores (solo lee).
- `python -m cnv ocr`: OCR opcional de los PDF escaneados (ver abajo).
- `python -m cnv clasificar`: asigna una categoría cerrada (`k`) a cada hecho relevante (ver abajo).
- `python -m cnv avisos-pago`: estructura los avisos de pago de ONs ya descargados (ver abajo).
- El workflow `Sincronizar documentos CNV` corre todos los días (y se puede disparar a mano para cargas grandes).

## Tipos de documento, topes y reintentos
- **Topes por documento** (un prospecto puede tener cientos de páginas): `--max-paginas` / `CNV_MAX_PAGINAS` (400), `--max-caracteres` / `CNV_MAX_CARACTERES` (1.500.000) y `--max-mb` / `CNV_MAX_MB` (80, tamaño del archivo descargado). Los topes se reparten entre todos los adjuntos del documento. El texto se corta en límite de página y **queda registrado**: el índice por empresa agrega `trunc` = `{"motivo": "paginas"|"caracteres", "paginas_total", "paginas_leidas"}` y el índice global agrega `tr` con el motivo (solo en los documentos truncados; el resto no cambia).
- **Descarga robusta:** cada adjunto se baja con hasta 4 intentos y espera creciente (3, 6, 9 s) ante cortes de red, 5xx o 429; los errores definitivos (404, archivo sobre el tope) no se reintentan. Cada documento tiene un tope de 15 minutos de reloj.
- **Resultados parciales:** si de dos adjuntos uno falla, se guarda el texto del que salió bien con `parcial` (cantidad de adjuntos fallidos) y `error` (motivo de cada uno). Se reintenta en corridas siguientes (hasta 3 intentos) y un reintento que traiga menos texto no pisa el que ya estaba. PDF cifrados con clave (no se pueden leer) y ZIP corruptos quedan como fallo con su motivo.
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
`cnv/texto/{cuit}/{doc_id}.txt.gz` · `cnv/indice/{cuit}.json` · `cnv/indice/global.json.gz` · `cnv/financieros/{cuit}.json` · `cnv/indice/financieros.json.gz` · `cnv/avisos_pago/{cuit}.json` · `cnv/indice/avisos_pago.json.gz`

## Cifras de estados contables (`cnv/financieros.py`)
Segunda etapa: de cada documento de tipo `estados_contables` con texto, saca periodo (cierre AAAA-MM-DD), tipo (anual/trimestral/semestral), si es acumulado, moneda y unidad, y las cifras `ventas`, `resultado_operativo`, `resultado_neto`, `activo_total`, `pasivo_total`, `patrimonio_neto`, `deuda_financiera` y `efectivo`.

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

## Configuración (repo)
- Secret `GCP_SA_KEY`: JSON de la cuenta de servicio con rol *Storage Object Admin* sobre el bucket.
- Variable `GCS_BUCKET`: nombre del bucket.
- Secret `ANTHROPIC_API_KEY`: solo para `financieros`, `resumenes`, `clasificar` y `avisos-pago`.

## Desarrollo
```
pip install -r requirements.txt
python -m pytest -q --cov=cnv --cov-fail-under=90     # 274 pruebas, ~99% de cobertura; sin red ni claves
DATOS_LOCAL=datos python -m cnv sync --desde 2026-09-01 --solo 30546689979 --max-docs 5
```

Sin `GCS_BUCKET` guarda en la carpeta local `datos/`. La información es pública; este proyecto solo la ordena. No es asesoramiento financiero.
