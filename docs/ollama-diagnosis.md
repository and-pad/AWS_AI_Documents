# Diagnóstico local de Ollama — 2026-10-08

No se desplegó, publicó una imagen ni modificó AWS. Las respuestas completas y los PDFs rasterizados de las pruebas permanecen únicamente en `/tmp/document-flow-diagnosis/`, fuera de Git, en un directorio accesible solo por su propietario.

## Recorrido comprobado

```text
PDF bytes
  → document_service._process_pdf
  → PyMuPDF.open(stream=..., filetype="pdf")
  → load_page(index).get_pixmap(dpi=180, alpha=False)
  → pixmap.tobytes("png")
  → _generate_validated(PageExtraction)
  → OllamaClient.chat: deepcopy(messages), base64 de PNG en messages[-1].images
  → POST /api/chat (stream=false, format=JSON Schema)
  → response.json()["message"]["content"] (no message.thinking)
  → json.loads + PageExtraction.model_validate
  → lista local de extracciones en orden
  → _generate_validated(DocumentAnalysisGeneration), sin imagen
  → DocumentAnalysis + user_request original
  → transcripción literal ensamblada desde PageExtraction (no desde síntesis)
  → Fargate SQS worker guarda results/{job_id}.json
```

No hay un system message separado ni ejemplos ficticios en los prompts. Cada operación construye su propio mensaje `user`. La extracción pide transcripción literal, `[ILEGIBLE]`, hechos relevantes, elementos visuales y contexto breve. La síntesis pide análisis conciso y respuesta al usuario basada exclusivamente en las extracciones. El retry solo añade instrucciones y rutas estructurales seguras; nunca reenvía contenido rechazado.

## Imagen comprobada

Primera página de TES_2025_0001.pdf:

| Rasterizador | DPI | Dimensiones | Bytes | SHA-256 |
| --- | ---: | --- | ---: | --- |
| PyMuPDF del processor | 180 | 1530 × 1980 | 1492991 | 931ef98189866180e9d92dc4cf470ae8310e9294f867e16fa2bf8ae9e08a9b09 |
| pdftoppm manual | 150 | 1275 × 1650 | 1047825 | ed181dd6bb57c94b46f3978a29bd2c5d303d471e978fc99393ade7f1b93c008e |

Las imágenes se inspeccionaron localmente: corresponden al oficio esperado. Los hashes difieren porque cambian DPI y rasterizador. No se cambió DPI. La rasterización y codificación PNG original tomó aproximadamente 0.2275 s; no explica minutos de generación.

Estructura enviada:

```json
{
  "model": "qwen3-vl:8b",
  "stream": false,
  "truncate": false,
  "shift": false,
  "messages": [{"role": "user", "content": "instrucciones de la página actual", "images": ["base64 de su PNG"]}],
  "format": "JSON Schema de PageExtraction",
  "options": {"num_ctx": 16384}
}
```

`format` es realmente un objeto JSON Schema, no el texto ilustrativo mostrado arriba. La síntesis omite `images` y usa su propio schema. No se configura `think` ni `num_predict` en producción; la temperatura normal sigue siendo la del modelo local (1). El único cambio de opciones es contexto explícito de 16384 (configurable mediante `OLLAMA_NUM_CTX`), junto a `truncate=false` y `shift=false`.

## Evidencia y límites

- No se encontraron `Universal Basic Income`, `triangle with sides`, `a + b = 12`, `journal article`, `inequality` ni `poverty` en código, prompts, fixtures o README antes de este diagnóstico. Tampoco en las respuestas temporales históricas disponibles.
- No hay conversación global, buffers de imagen compartidos, archivos temporales fijos ni importaciones de fixtures en el camino productivo. El caché de settings contiene únicamente configuración.
- Ollama registró truncamiento de contexto a 4096 tokens durante la prueba con schema: entrada de 5856 tokens reducida a 3866. Esto demuestra pérdida de contexto durante esa ejecución, pero no identifica por sí solo el origen de UBI/triángulo.
- La variante con temperatura cero reprodujo un timeout a 1200 s. Con `think=false` y `num_predict=4096`, Ollama devolvió HTTP 200 pero contenido vacío, 15110 caracteres de razonamiento y `done_reason=length`. Por eso no se adoptó ese límite ni se asumió que `think=false` funciona en esta versión/modelo.
- Dos respuestas anteriores disponibles tenían respectivamente 18534 y 7004 caracteres de razonamiento, con duraciones de 484.4 y 102.8 s. Ninguna contenía UBI/triángulo.
- No está disponible el envelope de la llamada histórica de 10m39s: no se conocen sus tokens, opciones exactas ni estado de caché. No es posible atribuir esa duración exacta a una sola causa retrospectivamente.
- AWS rechazó la consulta de lectura con `InvalidClientTokenId`; no se pudo inspeccionar el resultado histórico desplegado. No se encontró una credencial local para probar el endpoint HTTPS autenticado; no se solicitó ni imprimió el token.
- Un schema válido demuestra forma y tipos, no exactitud factual. Las pruebas reales de ambos oficios y los tests de aislamiento permiten comprobar el camino actual; no garantizan ausencia de alucinaciones para documentos arbitrarios.

## Correcciones

- Contexto explícito 16384 configurable: la extracción PageExtraction y la síntesis final pasaron cada una bajo `truncate=false`, `shift=false`, en pruebas reales. Contexto 8192 generó contenido vacío al activar esas protecciones.
- Retry conserva sampling normal, en vez de forzar la temperatura cero que reprodujo el bloqueo.
- Imagen y todas las instrucciones se mantienen en el mismo mensaje en el retry. Se reconstruyen mensajes y se clonan estructuras anidadas.
- Regla explícita de usar únicamente la imagen y no completar texto a partir del contexto anterior.
- La síntesis ya no pide al VLM volver a emitir `transcription`; el backend ensambla esa parte literalmente desde `PageExtraction`. El JSON público conserva `analysis`, `transcription`, `visual_elements` y `requested_output`.
- Logs por job/página: dimensiones, bytes, hash corto, tamaño de prompt, conteo de mensajes, duración, conteos de tokens, finalización y longitud del razonamiento. Sin imágenes, base64, tokens, texto documental ni prompts completos.
- Se conservan Pydantic, JSON Schema, máximo dos intentos, salida final de cuatro áreas, arquitectura SQS → Fargate y `DEBUG_MODEL_OUTPUT=false`.

16384 no es contexto ilimitado. Documentos extensos aún requieren evaluar memoria y longitud de la síntesis; no se hizo una reescritura del procesamiento multipágina.

## Experimentos con la misma imagen de 180 DPI

A = prompt simple sin schema; B = prompt original de página sin schema; C = prompt original con schema y defaults; D = C con temperatura cero; E = D con think=false (cancelado); F = E con num_predict=4096; G = C cambiando solo num_ctx=8192. H es síntesis aislada, sin imagen, con el schema original y contexto 8192.

| Prueba | Prompt | Schema | Options / think | Segundos | prompt_eval_count | eval_count | Contenido | `done_reason` | Resultado |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| A | Simple, 167 chars | No | Default (`think` no enviado) | 57.41 | 2040 | 1175 | 2048 | stop | Texto correcto, `invalid_json` esperado sin schema |
| B | Página, 1006 chars | No | Default | 153.65 | 2264 | 4068 | 2582 | stop | JSON, `schema_invalid` |
| C | Página, 1006 chars | PageExtraction | Default, contexto Ollama 4096 | 188.66 | 3866 | 751 | 2509 | stop | Válido |
| D | Igual a C | PageExtraction | `temperature=0` | 1200.11 | — | — | — | — | `read_timeout` |
| E | Igual a C | PageExtraction | `temperature=0`, `think=false`; cancelada | 219.74 | — | — | — | — | Sin envelope final |
| F | Igual a E | PageExtraction | `temperature=0`, `think=false`, `num_predict=4096` | 167.97 | 2262 | 4096 | 0 | length | Razonamiento agotó el presupuesto; inválido |
| G | Igual a C | PageExtraction | `num_ctx=8192`, defaults | 113.53 | 3585 | 957 | 3033 | stop | Válido; no desactiva el recorte |
| H | Síntesis, sin imagen | DocumentAnalysisGeneration | `num_ctx=8192`, defaults | 258.89 | 6867 | 867 | 2699 | stop | Válido; no desactiva el recorte |
| I | Igual a C | PageExtraction | `num_ctx=8192`, `truncate=false`, `shift=false` | 243.07 | 2290 | 5902 | 0 | length | Rechazado como `missing_content` |
| J | Igual a C | PageExtraction | `num_ctx=16384`, `truncate=false`, `shift=false` | 260.67 | 5808 | 883 | 2736 | stop | Válido; folio esperado |
| K | Síntesis real de TES_2025_0002, sin imagen | DocumentAnalysisGeneration anterior | `num_ctx=16384`, `truncate=false`, `shift=false` | 201.72 | 4145 | 1007 | 3166 | stop | Válido |
| L | PageExtraction TES_2025_0001 con código actual | PageExtraction | `num_ctx=16384`, `truncate=false`, `shift=false` | 125.75 | 3558 | 903 | 2786 | stop | Válido; una imagen, SHA anterior |
| M | Síntesis TES_2025_0001 con schema actual sin transcripción | DocumentAnalysisGeneration | `num_ctx=16384`, `truncate=false`, `shift=false` | 324.22 | 6741 | 480 | 1548 | stop | Válido; 3 campos raíz |

Entorno: Ollama 0.24.0, qwen3-vl:8b Q4_K_M, digest `901cae73216286ea8c5aba8b46d307ff7188f737285ec500c795a12f05225d28`, GPU GTX 1070.

## Publicación posterior (no ejecutada)

Primero restaura tu sesión AWS válida. Estos comandos utilizan los Outputs y parámetros reales del stack, sin inventar nombres físicos ni poner el token de Ollama en la consola. Revisa previamente que los otros cambios pendientes del repositorio también estén autorizados para ese despliegue.

```bash
export AWS_REGION=us-east-2
STACK=document-ai-pipeline
TAG=processor-grounding-20261009

ECR_URI=$(aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='DocumentProcessorRepositoryUri'].OutputValue" --output text)
CLUSTER=$(aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='DocumentProcessorClusterName'].OutputValue" --output text)
SERVICE=$(aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='DocumentProcessorServiceName'].OutputValue" --output text)

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${ECR_URI%%/*}"
docker build --platform linux/amd64 -t "$ECR_URI:$TAG" ./document-processor
docker push "$ECR_URI:$TAG"

# Preservar todos los parámetros desplegados; cambiar únicamente el tag.
aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK" \
  --query 'Stacks[0].Parameters' --output json > /tmp/document-ai-deploy-parameters.json
mapfile -t DEPLOY_PARAMETERS < <(python3 - "$TAG" <<'PY'
import json, sys
from pathlib import Path
parameters = {item['ParameterKey']: item['ParameterValue']
              for item in json.loads(Path('/tmp/document-ai-deploy-parameters.json').read_text())}
parameters['DocumentProcessorImageTag'] = sys.argv[1]
for key, value in parameters.items():
    print(f'{key}={value}')
PY
)
python3 infrastructure/aws/assemble_template.py
sam build --template-file infrastructure/aws/.template-assembled.yml
sam deploy --region "$AWS_REGION" --stack-name "$STACK" \
  --template-file .aws-sam/build/template.yaml \
  --config-file infrastructure/aws/samconfig.toml \
  --parameter-overrides "${DEPLOY_PARAMETERS[@]}"
aws ecs wait services-stable --region "$AWS_REGION" --cluster "$CLUSTER" --services "$SERVICE"
aws ecs describe-services --region "$AWS_REGION" --cluster "$CLUSTER" \
  --services "$SERVICE" --query 'services[0].deployments'
```

El nuevo tag registra una revisión de TaskDefinition y SAM inicia el rollout; no requiere otro `force-new-deployment`. Si posteriormente necesitas forzar explícitamente un reinicio de esa misma revisión:

```bash
aws ecs update-service --region "$AWS_REGION" --cluster "$CLUSTER" \
  --service "$SERVICE" --force-new-deployment
aws ecs wait services-stable --region "$AWS_REGION" --cluster "$CLUSTER" --services "$SERVICE"
```

No cambió IAM ni recursos stateful. La plantilla agrega el parámetro `OllamaNumCtx` y el TaskDefinition pasa `OLLAMA_NUM_CTX`; CloudFormation creará una nueva revisión del TaskDefinition. El default 16384 pasó PageExtraction y síntesis reales en la GTX 1070, con una capa del modelo en CPU. No se modifican certificados, Apache, Secrets Manager ni configuración del servidor Ollama.

## Transformaciones Python comprobadas

El cliente solo quita whitespace exterior de `message.content`; conserva el string original para el debug opt-in. No corta transcripción ni usa `message.thinking`. `context_summary` sí se limita explícitamente a `CONTEXT_MAX_CHARS` para continuidad entre páginas; esa función no modifica la transcripción. La síntesis recibe el JSON de la lista local de extracciones validadas. El backend conserva `user_request` original, fuerza response null si no hubo solicitud y ensambla el texto literal con separadores de página. Fargate serializa el dict una sola vez con `json.dumps(...).encode("utf-8")`; no almacena un string JSON dentro de otro JSON. No se modificó este formato de ResultsBucket ni su lectura/renderizado.

La síntesis original volvía a pedir al modelo una transcripción que después el backend descartaba. El schema actual ya no contiene ese campo: es trabajo redundante eliminado sin cambiar la respuesta pública ni la transcripción literal ensamblada por el backend. La latencia que queda corresponde a una segunda inferencia de análisis, separada de la extracción de cada página.

## Diferencia concreta dentro de Ollama 0.24.0

Se comprobó el código de la versión instalada, no solo una incidencia de otra versión:

- [server/routes.go, v0.24.0](https://github.com/ollama/ollama/blob/v0.24.0/server/routes.go#L2486-L2656) inicia una generación sin `format` cuando el modelo tiene capacidad de razonamiento. Al aparecer contenido, cancela esa generación, añade un mensaje assistant con el razonamiento y vuelve a ejecutar el modelo con `format`.
- [server/prompt.go, v0.24.0](https://github.com/ollama/ollama/blob/v0.24.0/server/prompt.go#L20-L114) puede retirar mensajes anteriores para que entren en contexto; solo garantiza conservar el último mensaje y los mensajes system. En el segundo pase, el último mensaje es el razonamiento del assistant, no necesariamente la imagen/instrucciones originales del usuario.
- La respuesta no streaming conserva las métricas de la última generación, mientras `total_duration` abarca toda la llamada. Por eso un `eval_count` pequeño no demuestra que el trabajo completo haya sido pequeño; explica la diferencia observada entre duración total y `eval_duration`.
- La llamada simple sin `format` no hace ese reinicio estructurado. El processor hace extracción y síntesis: ambas pueden implicar este trabajo interno adicional, antes de cualquier retry de aplicación.

Esto demuestra un mecanismo concreto capaz de perder grounding con contexto corto. **No demuestra retrospectivamente que haya causado los textos UBI/triángulo específicos**, cuyos resultados desplegados no pudieron recuperarse.

Además del contexto 16384, el cliente ahora envía `truncate=false` y `shift=false` en llamadas estructuradas. Así un exceso de contexto se rechaza en vez de perder silenciosamente mensajes o desplazar contexto durante generación. PageExtraction y síntesis reales pasaron con esas protecciones a contexto 16384. No se reduce `num_predict` ni se cambian timeouts.

Existe una [corrección upstream de Ollama para realizar una sola generación](https://github.com/ollama/ollama/commit/5a0ff3116d7d1aff28cd7a390783d809f69f0b6c). No se actualizó el servidor ni se afirma que una versión arbitraria incorpore ese cambio; cualquier actualización futura debe comprobarlo y volver a medir.

### Prueba para evitar recorte de entrada

Con `truncate=false`, `shift=false`, schema y la misma imagen de TES_2025_0001:

| Contexto | Tiempo | HTTP | `done_reason` | Contenido | Validación | Identificador esperado |
| ---: | ---: | ---: | --- | ---: | --- | --- |
| 8192 | 243.1 s | 200 | `length` | 0 caracteres | Rechazado como `missing_content` | — |
| 16384 | 260.7 s | 200 | `stop` | 2736 caracteres | PageExtraction válida | Sí |

La reproducción estricta con schema de síntesis a 8192 devolvió HTTP 200, `done_reason=length` y contenido vacío, rechazado como `missing_content`. La misma síntesis real, con contexto 16384 y sin truncamiento ni desplazamiento, se validó en 201.72 s, `done_reason=stop`, 3166 caracteres. El presupuesto de 16k permite la salida en la prueba PageExtraction pese a que Ollama coloca la capa de salida en CPU en la GTX 1070. No se aumentó `num_predict` ni se cambió timeout.

## Tiempo por etapa: pipeline completo local

Estas dos ejecuciones recorrieron `_process_pdf` completo con las imágenes y extracciones del PDF correspondiente. Usaron el contexto 8192 y el comportamiento de truncamiento predeterminado de Ollama; son mediciones antes de activar la protección `truncate=false`/`shift=false`, no una medición estricta del nuevo contexto 16384. El muestreo incluye PageExtraction, validación y síntesis; no incluye subida a S3.

| Documento | Rasterización PNG | PageExtraction HTTP | Validación Pydantic | Síntesis HTTP | Serialización JSON | Total `process_pdf` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| TES_2025_0001 (1 página) | 0.070 s | 146.79 s | 0.00011 s | 184.67 s | 0.00007 s | 331.71 s |
| TES_2025_0002 (1 página) | ~0.07 s | 193.61 s | <0.001 s | 174.71 s | <0.001 s | 368.60 s |

La tabla muestra que el coste dominante son las dos llamadas Ollama, no PDF→PNG ni Pydantic/JSON. La prueba A es la comparación directa y ligera (57.41 s); no equivale al análisis completo. Después de las protecciones y de retirar `transcription` del schema de síntesis, se volvió a ejecutar localmente todo TES_2025_0001 con el código actual: total 450.22 s; raster 0.228 s; PageExtraction 125.75 s; síntesis 324.22 s. Ambas respuestas validaron. El resultado final tuvo transcripción ensamblada desde la página, no una transcripción del modelo de síntesis. Un run anterior midió Pydantic en 0.00011 s y serialización del resultado en 0.00007 s; ambas son despreciables frente a Ollama.

En esta ejecución actual, PageExtraction tuvo `done_reason=stop`, `eval_count=903`, `eval_duration=45.85 s` y `total_duration=125.74 s`. Síntesis tuvo `done_reason=stop`, `eval_count=480`, `eval_duration=25.41 s` y `total_duration=324.22 s`; el envelope informó 19681 caracteres en `message.thinking`. Las métricas de evaluación corresponden al pase final y no explican por sí solas todo el tiempo. No hubo truncamiento, JSON inválido ni fallo de schema.
