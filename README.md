# gbr-cnv-documentos

Descarga los documentos **públicos** que las empresas argentinas presentan en la CNV (hechos relevantes, estados contables, prospectos y suplementos), extrae su texto y lo guarda en Google Cloud Storage para que el asistente de [GBR Inversiones](https://gbr-inversiones.web.app/herramientas/asistente-renta-fija) pueda buscarlos y responder preguntas sobre cada uno.

## Cómo funciona
- `data/universo.csv`: empresas (acciones y emisores de ONs que cotizan en BYMA) con su CUIT.
- `python -m cnv sync`: lista los documentos de cada empresa, baja los que faltan, extrae el texto de los PDF (también los que vienen en ZIP) y guarda el texto y un índice por empresa.
- `python -m cnv indice`: arma el índice global que usa el buscador.
- `python -m cnv financieros`: extrae cifras clave de los estados contables ya descargados (ver abajo).
- El workflow `Sincronizar documentos CNV` corre todos los días (y se puede disparar a mano para cargas grandes).

## Almacenamiento
`cnv/texto/{cuit}/{doc_id}.txt.gz` · `cnv/indice/{cuit}.json` · `cnv/indice/global.json.gz` · `cnv/financieros/{cuit}.json` · `cnv/indice/financieros.json.gz`

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

## Configuración (repo)
- Secret `GCP_SA_KEY`: JSON de la cuenta de servicio con rol *Storage Object Admin* sobre el bucket.
- Variable `GCS_BUCKET`: nombre del bucket.
- Secret `ANTHROPIC_API_KEY`: solo para `python -m cnv financieros`.

## Desarrollo
```
pip install -r requirements.txt
pytest
DATOS_LOCAL=datos python -m cnv sync --desde 2026-09-01 --solo 30546689979 --max-docs 5
```

Sin `GCS_BUCKET` guarda en la carpeta local `datos/`. La información es pública; este proyecto solo la ordena. No es asesoramiento financiero.
