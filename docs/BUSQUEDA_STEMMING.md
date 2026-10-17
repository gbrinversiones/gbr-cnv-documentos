# Propuesta (NO activa): stemming ligero en español para el buscador BM25

**Estado: propuesta documentada, sin implementar ni activar.** El índice (`cnv/busqueda.py`) y el lector Node (`functions/lib/busqueda.js` en gbr-landing) comparten una tokenización exacta; cambiarla desincroniza un lado del otro. Decisión de Main Local (17/10/2026): no se activa, no vale el riesgo.

## Qué mejoraría
Hoy la tokenización es: NFD sin marcas → minúsculas → `/[a-z0-9]+/` → largo 3–30 → sin stopwords, **sin stemming**. «emisión», «emisiones» y «emitió» son tres términos distintos; una consulta «emisiones de obligaciones negociables» no encuentra documentos que dicen «emisión de obligación negociable» salvo por los otros términos.

## Reglas candidatas (solo sufijos, sobre el token ya normalizado, largo mínimo del resto 4)
1. `-ciones` / `-siones` → `-cion` / `-sion` (emisiones → emision).
2. Plural simple: `-es` tras consonante y `-s` tras vocal → se quita (obligaciones → obligacion, negociables → negociable, ons → ons).
3. `-mente` → se quita si queda ≥ 4 letras (unicamente → unica).
4. Sin tocar números ni tokens con dígitos (`2035`, `a3`), ni siglas de ≤ 3 letras.

## Qué hay que hacer a la vez (si algún día se activa)
1. Mismo algoritmo en `cnv/busqueda.py::tokenizar` y en `busqueda.js`, con una función única `stem(token)` y la **lista de casos** como fixture compartido (`tests/fixtures/busqueda_stemming.json`: token → stem esperado) que corran los tests de los dos lados.
2. Subir `VERSION` del índice a 2 (el lector rechaza versiones que no conoce: `meta.json` con `version != 1` hoy devuelve vacío) y reconstruir **todo** el índice (los shards dependen de `crc32(termino)`; los términos cambian).
3. Regenerar el oráculo (`tests/generar_oraculo.py` → `tests/fixtures/busqueda_oraculo.json`) y el test cruzado Python/Node.
4. Publicar en este orden para que ninguna versión del lector lea un índice que no entiende: (a) desplegar el lector que acepta v1 y v2; (b) reconstruir el índice en v2; (c) retirar v1 del lector cuando ya no haga falta.

## Riesgos
- Un stem agresivo une palabras distintas (p. ej. «mercado» / «mercados» está bien; «obligación» / «obligado» no deberían unirse: las reglas de arriba no las tocan).
- Documentos y consultas deben pasar por el **mismo** stem; un desfase (lector viejo, índice nuevo) rompe la búsqueda sin error visible.
- No hay una medición de relevancia (consultas reales con respuestas esperadas) que muestre cuánto mejoraría; antes de activar conviene armar 20–30 consultas con los documentos esperados y comparar nDCG o simplemente aciertos en el top 10.

## Alternativa sin riesgo de desincronización
Expandir la **consulta** (no el índice) en el lector: probar también las variantes de la consulta (singular/plural, `-cion`/`-ciones`) y sumar sus puntajes. Solo se toca el lector Node; el índice y su contrato quedan como están. Menos preciso que un stem real, pero reversible y sin reconstruir nada.
