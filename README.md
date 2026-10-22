# AWS AI Documents

Pipeline asíncrono para analizar PDFs por página con `qwen3-vl:8b-instruct` servido por Ollama.

## Estructura relevante

```text
frontend/                         React + Vite: Cognito, alta de trabajos, carga y polling
document-processor/app/
  main.py                         FastAPI /health, /process y ciclo de vida del consumidor SQS
  sqs_worker.py                   Long polling SQS → lease DynamoDB → proceso directo → S3
  document_service.py             PDF → imágenes → análisis secuencial → consolidación
  ollama_client.py                Cliente HTTP aislado para Ollama
  settings.py                     Variables de entorno y límites
worker/app/
  jobs.py                         API de alta/consulta y URLs S3 prefirmadas
  worker.py                       Consumidor Lambda legado, sin trigger productivo
infrastructure/aws/template.yml   Manifiesto SAM con parámetros globales, Outputs y módulos de recursos
infrastructure/aws/stacks/        Fragmentos por dominio; el ensamblador conserva un solo stack CloudFormation
infrastructure/aws/assemble_template.py  Ensambla los fragmentos para SAM sin cambiar Logical IDs
compose.yaml                      document-processor local
```

## Flujo y decisiones

1. React registra e inicia sesión con Cognito. Las sesiones y su renovación las conserva Amplify en el navegador; React manda el ID token a API Gateway. El Cognito User Pool Authorizer valida el token y JobsApi toma `sub` de los claims autorizados.
2. React hace `POST /jobs` únicamente con nombre, tamaño y tipo; no pide una instrucción personalizada durante la carga. JobsApi valida el PDF/tamaño, crea un ID y guarda `user_id=<sub>` en DynamoDB; devuelve una presigned PUT URL de corta duración para `users/{sub}/jobs/{job_id}/{filename}`.
3. React sube directamente a S3 sin credenciales AWS. La presigned URL exige tipo/tamaño y metadata `x-amz-meta-job-id`. Tras un PUT exitoso, React comienza polling. JobsApi comprueba el objeto y su metadata en el primer `GET /jobs/{job_id}`, verifica ownership y marca el trabajo `QUEUED`; así un job ya subido no permanece etiquetado `UPLOAD_PENDING` mientras espera turno en SQS.
4. S3 envía el evento a `DocumentQueue`. Una task Fargate inicia `PROCESSOR_MAX_CONCURRENT_JOBS` consumidores SQS independientes (1 por defecto, máximo 2); cada consumidor hace long polling de un mensaje y no recibe otro hasta terminar el actual. No se reserva un tercer mensaje mientras los dos slots estén ocupados. La task procesa el PDF directamente en el contenedor; no hay una Lambda esperando una llamada HTTP larga. La transición condicional y la lease DynamoDB impiden que mensajes duplicados o una task nueva durante un despliegue inicien el mismo job a la vez. Las keys antiguas `jobs/{job_id}/{filename}` siguen siendo reconocidas.
5. `document-processor` abre el PDF con PyMuPDF, rasteriza una página cada vez, envía la imagen a Ollama y produce el análisis página a página. Al pedir cada página, incluye un `context_summary` limitado a `CONTEXT_MAX_CHARS`; el modelo lo actualiza con entidades, datos del documento y continuidad relevantes. Los análisis detallados no se reutilizan como contexto y se conservan completos para la consolidación final. La síntesis inicial es neutral: no depende de una pregunta del usuario.
6. La task guarda el resultado JSON bajo la clave determinística `results/{job_id}.json`, incluidos los campos generales y las extracciones validadas por página, confirma la escritura, persiste `COMPLETED` y `result_key` en DynamoDB y solo entonces elimina el PDF de `DocumentsBucket`. Un retry antes de actualizar DynamoDB vuelve a escribir la misma clave y reemplaza el objeto existente. Si el procesamiento falla, no borra el mensaje ni el PDF: antes del tercer receive vuelve el job a `QUEUED`; en el tercero queda `FAILED` y SQS lo redirige a la DLQ. La API `GET /jobs/{job_id}` solo entrega el resultado si el `sub` coincide con `user_id`.
7. Cada documento `COMPLETED` ofrece **Preguntar a la IA**. `POST /jobs/{job_id}/ask` valida Cognito/ownership, crea un `ask_id` y encola solo identificadores en `QuestionQueue`; una Lambda separada lee `results/{job_id}.json`, selecciona fragmentos pertinentes y consulta Ollama. React consulta `GET /jobs/{job_id}/asks/{ask_id}` hasta un estado terminal. Esta ruta no vuelve a abrir el PDF ni ejecuta PageExtraction/FinalSynthesis; la cola de preguntas está separada de `DocumentQueue`.

El bucket de resultados está separado del bucket de entrada: este último envía eventos S3 a SQS, por lo que guardar el resultado en el mismo bucket volvería a activar el worker. Los jobs/resultados se conservan hasta que el usuario elimina el job. Los PDFs son temporales: el worker los elimina al completar y el lifecycle de `DocumentsBucket` expira objetos restantes después de 30 días; es solo un fallback para objetos huérfanos y deja margen para esperas, reintentos y mensajes en DLQ.

## Ollama y document-processor local

Requiere Docker Compose, Ollama en el host local de desarrollo y el modelo descargado. El siguiente bind `0.0.0.0` aplica solo a ese entorno local para que Docker llegue al host; el servidor Ollama remoto de producción permanece en `127.0.0.1:11434` detrás de Apache:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
ollama pull qwen3-vl:8b-instruct
curl http://localhost:11434/api/tags
```

En otra terminal, desde la raíz:

```bash
cp .env.example .env
docker compose up --build -d
curl http://localhost:8000/health
docker compose logs -f document-processor
```

En Docker Desktop `host.docker.internal` suele estar disponible. En Linux Compose añade `host.docker.internal:host-gateway`, así que `OLLAMA_BASE_URL=http://host.docker.internal:11434` apunta al host sin fijar una IP. También se acepta `OLLAMA_URL` como alias para archivos `.env` anteriores. Si Ollama corre en otro contenedor de la misma red Docker, configura `OLLAMA_BASE_URL=http://ollama:11434`; no se instala Ollama dentro de `document-processor`.

Las variables disponibles son:

| Variable | Predeterminado | Uso |
| --- | --- | --- |
| `OLLAMA_BASE_URL` | `http://localhost:11434` en proceso directo; Compose lo establece a `http://host.docker.internal:11434` | Base URL de Ollama; local/AWS se configura sin cambios de código |
| `OLLAMA_URL` | Alias legado de `OLLAMA_BASE_URL` | Compatibilidad con `.env` anteriores |
| `OLLAMA_API_TOKEN` | Vacío en desarrollo local | Bearer token opcional; en AWS ECS lo inyecta Secrets Manager |
| `OLLAMA_MODEL` | `qwen3-vl:8b-instruct` | Modelo Ollama |
| `CORS_ORIGINS` | `http://localhost:5173` | Orígenes permitidos por FastAPI |
| `MAX_UPLOAD_BYTES` | `20971520` | Máximo 20 MiB |
| `PDF_RENDER_DPI` | `180` | Resolución de render de páginas |
| `CONTEXT_MAX_CHARS` | `3000` | Límite del contexto resumido |
| `DEBUG_MODEL_OUTPUT` | `false` | Diagnóstico sensible temporal: solo ante JSON/schema inválido registra en CloudWatch el `message.content` completo de Ollama y metadata de validación. Mantener desactivado salvo una prueba controlada; la retención del log group Fargate es 7 días. |
| `OLLAMA_READ_TIMEOUT` | `1800` | Límite de una llamada individual a Ollama; no limita la duración total del PDF. |
| `OLLAMA_NUM_CTX` | `16384` | Contexto enviado en cada extracción/síntesis; incluye imagen, schema, entrada y razonamiento. Ajustarlo requiere medir memoria y calidad en el servidor Ollama. El proxy rechaza el truncamiento/desplazamiento en solicitudes estructuradas. |
| `OLLAMA_PAGE_NUM_PREDICT` | `6000` | Presupuesto máximo de tokens generados para cada página. |
| `OLLAMA_SYNTHESIS_NUM_PREDICT` | `2000` | Presupuesto máximo para la síntesis final. |
| `SQS_VISIBILITY_TIMEOUT` | `900` | Ventana de visibilidad renovable del mensaje de producción. |
| `SQS_VISIBILITY_HEARTBEAT_SECONDS` | `300` | Intervalo de renovación de visibilidad y lease. |
| `PROCESSOR_MAX_CONCURRENT_JOBS` | `1` | Consumidores independientes por task Fargate; admite `1` o `2`. Cada job conserva sus páginas secuenciales. |

El endpoint multipart local/diagnóstico es `POST /process` con el campo `file`; las solicitudes viejas con `user_prompt` siguen siendo compatibles para trabajos ya persistidos, pero la interfaz nueva no envía preguntas durante la carga. `/health` no llama a Ollama. Los errores del proveedor se registran y `/process` contesta 502 sin stack trace.

Cada extracción y su retry usan una sola imagen de la página actual y mensajes nuevos. El retry de JSON/schema agrega instrucciones de corrección al mismo mensaje, sin reenviar la salida rechazada y sin forzar temperatura cero; una respuesta marcada por Ollama como limitada/incompleta no se repite inmediatamente con el mismo presupuesto. JSON Schema y Pydantic siguen validándose antes de guardar checkpoints. `OLLAMA_PAGE_NUM_PREDICT` y `OLLAMA_SYNTHESIS_NUM_PREDICT` limitan independientemente la salida de páginas y síntesis; `done_reason=length`, `done=false` o alcanzar el presupuesto sin una señal explícita de parada normal rechaza la salida antes de validarla o persistirla. Una terminación `stop` normal se acepta aunque `eval_count` esté cerca del presupuesto. Sin metadata de terminación, el sistema conserva compatibilidad y valida JSON/Pydantic, pero no puede confirmar truncamiento. Los logs incluyen presupuesto, metadata, estado de terminación y categoría sin transcripción, prompt, razonamiento, token ni respuesta completa. `DEBUG_MODEL_OUTPUT` permanece desactivado.

Después de cada extracción válida, el worker guarda un checkpoint privado e inmutable en `ResultsBucket/checkpoints/{job_id}/` y escribe un manifiesto inmutable. DynamoDB conserva únicamente la key del último manifiesto comprometido bajo la lease actual; no guarda transcripciones. Una entrega SQS posterior valida el manifiesto y los checkpoints contra el PDF descargado, la versión Pydantic y la huella de configuración; solo recupera una secuencia contigua desde la página 1. Si falta o no valida una página, se vuelve a extraer desde esa página. La síntesis usa las extracciones recuperadas y nuevas en orden, por lo que un fallo de síntesis solo repite esa etapa. Al persistir `COMPLETED`, se eliminan los checkpoints; una regla Lifecycle limitada a `checkpoints/` los expira tras 30 días como respaldo. `results/` no tiene esa expiración. Los checkpoints no contienen imágenes ni se exponen en la API.

### Diagnóstico después de una prueba AWS

Después de que un job termine o falle, ejecuta una sola vez:

```bash
AWS_PROFILE=document-ai-ohio python3 scripts/diagnose_job.py <JOB_ID> \
  --stack document-ai-pipeline --region us-east-2 --window-minutes 120
```

El script descubre nombres físicos desde CloudFormation, lee el item DynamoDB, métricas SQS, eventos/logs ECS/Lambda, tasks recientes y métricas CPU/memoria. No llama `ReceiveMessage`, no cambia visibilidad y no mantiene polling. Limita los logs a una ventana y hasta 500 eventos por consulta. Genera un reporte local con permisos `0600`; si el debug sensible estuvo activo, el reporte puede contener la respuesta completa de Ollama. Compártelo únicamente por un canal privado. Requiere permisos de solo lectura para CloudFormation describe, DynamoDB `GetItem`, SQS `GetQueueAttributes`, ECS list/describe, CloudWatch Logs `FilterLogEvents` y CloudWatch `GetMetricStatistics`.

Para habilitar temporalmente la captura exacta en CloudWatch, despliega `DebugModelOutput=true` únicamente durante una prueba controlada y vuelve a desplegarlo como `false` al terminar. El contenido se conserva en el log group ECS existente, que CloudFormation configura con 7 días de retención; el Task Execution Role solo escribe allí y no recibe permisos de lectura nuevos. CloudWatch no permite borrar un evento individual: si se requiere borrado inmediato, eliminar el log stream de la task afectada elimina todos sus logs; de otro modo la retención los expira automáticamente.

Las llamadas estructuradas envían `truncate=false` y `shift=false`: un contexto insuficiente debe fallar explícitamente en vez de descartar la imagen/entrada y producir JSON sin fundamento. En Ollama 0.24.0 el modo estructurado con razonamiento reconstruye internamente el prompt para una segunda generación, por lo que el contexto debe incluir también ese razonamiento.

Las mediciones reales, limitaciones del diagnóstico y comandos de publicación posteriores están en [docs/ollama-diagnosis.md](docs/ollama-diagnosis.md).

## Frontend

Instala dependencias una vez y configura las salidas de SAM en `frontend/.env.local`:

```bash
cd frontend
npm install
cp .env.example .env.local
# configura JobsApiUrl, CognitoUserPoolId y CognitoUserPoolClientId desde los outputs
npm run dev
```

Abre `http://localhost:5173`. El cliente Cognito es público y no tiene secret. El navegador solo conoce el API, los identificadores públicos de Cognito, los JWT y la URL temporal prefirmada; no recibe credenciales AWS.

### CORS por entorno

`FrontendOrigin` configura el origen permitido tanto en el preflight de API Gateway y las respuestas de `JobsApiFunction` como en el PUT directo a `DocumentsBucket`. LocalStack lo fija explícitamente en `http://localhost:5173` desde `infrastructure/aws/deploy-local.sh`. Para AWS, el default y `samconfig.toml` usan `https://dfw9n3vcykvho.cloudfront.net`; puede sobrescribirse en el despliegue con `FrontendOrigin=<origen>`.

El formulario permite enviar otro PDF en cuanto termina el PUT anterior, sin esperar a que concluya el análisis. Mantiene una lista/polling independiente por job para que A, B y C puedan estar visibles a la vez.

## SAM y despliegue

`template.yml` es el manifiesto fuente. `stacks/` separa los recursos por dominio y `assemble_template.py` los combina en `.template-assembled.yml`, un único template SAM normal. El ensamblado mantiene los 35 Logical IDs en el mismo stack `document-ai-pipeline`; no crea nested stacks ni cambia la propiedad de los recursos. Las referencias entre módulos siguen siendo referencias CloudFormation directas. La plantilla generada está ignorada por Git.

El comando local `lstk sam` de este entorno no admite nested stacks; este ensamblado también permite mantener el flujo actual de LocalStack. Para validar y construir, primero genera el template:

```bash
python3 infrastructure/aws/assemble_template.py
sam validate --lint --template-file infrastructure/aws/.template-assembled.yml
sam build --template-file infrastructure/aws/.template-assembled.yml
```

Desde la raíz del repositorio:

```bash
bash infrastructure/aws/deploy-local.sh
```

El script obtiene el gateway de la red Docker `bridge` y lo configura como `DocumentProcessorUrl`; LocalStack conserva el consumidor Lambda legado y el puerto 8000 del processor está publicado en el host. También selecciona `ProcessorDeployment=local`, que evita crear ECR/VPC/ECS. En AWS con `ProcessorDeployment=fargate`, CloudFormation elimina el EventSourceMapping de `DocumentProcessorFunction`: la task Fargate consume la cola directamente.

En producción `QueueVisibilityTimeout=900` es una ventana de recuperación, no el máximo de duración del documento. El consumidor renueva la visibilidad y su lease cada 300 segundos; si una task muere, ambos vencen y SQS entrega el mensaje a una task nueva.

Para LocalStack, arráncalo o reinícialo con el perfil del repositorio:

```bash
lstk --config infrastructure/aws/lstk-local.toml start
# si ya está iniciado:
lstk --config infrastructure/aws/lstk-local.toml restart
```

La plantilla mantiene Cognito y el User Pool Authorizer habilitados; no existe bypass local. LocalStack no tiene una licencia compatible con Cognito en este entorno. La parte de autenticación se desplegará/probará posteriormente en AWS real. LocalStack tampoco envía emails reales por defecto.

La tabla conserva `job_id` como PK para que API y worker sigan usando sus lecturas/actualizaciones actuales. Se añadió `UserJobsIndex` con PK `user_id` y SK `created_at` para permitir listados futuros con Query, sin Scan. Los registros previos al cambio no tienen `user_id`; quedan inaccesibles por la nueva API hasta que se decida una migración de propietarios.

`GET /jobs` requiere el mismo Cognito Authorizer y consulta `UserJobsIndex` con el `sub` validado. Devuelve los 100 trabajos más recientes del usuario, `count` total y `limit`; `has_more` indica si DynamoDB tiene más resultados, aunque la interfaz inicial todavía no ofrece paginación. No devuelve claves de S3, prompts ni datos de infraestructura. Al recargar, React vuelve a pedir el historial y obtiene cada detalle/resultado de los trabajos `COMPLETED` mediante `GET /jobs/{job_id}`.

Cada `POST /jobs` reserva un slot mediante una transacción DynamoDB que incrementa el contador privado del usuario y crea el job atómicamente. El item contador usa `job_id=__user_quota__#{sub}` y no tiene `user_id`/`created_at`, así que no aparece en `UserJobsIndex`. Al actualizar una instalación que ya tiene jobs, la primera consulta del usuario inicializa el contador mediante paginación de `Query` sobre el GSI; el GSI es eventualmente consistente, por lo que conviene permitir que la propagación de trabajos recientes termine antes de desplegar el límite. El parámetro `MaxUserJobs` configura el valor (20 por defecto).

`DELETE /jobs/{job_id}` verifica ownership, marca el job `DELETING`, elimina por sus claves guardadas el JSON `results/{job_id}.json` (si tiene `result_key`) y el PDF original si aún existe, y por último elimina el job y libera el slot en una transacción DynamoDB. No enumera objetos S3. Si falla S3, `DELETING` conserva el job y su slot para reintentar; si falla DynamoDB después de borrar S3, repetir DELETE es seguro porque `DeleteObject` es idempotente. El worker usa transiciones condicionales para no comenzar o resucitar jobs marcados/eliminados. Jobs `PROCESSING`, `QUEUED` o en otros estados no terminales no se pueden borrar. Un `UPLOAD_PENDING` solo se puede borrar pasados 20 minutos (15 minutos de vigencia de la presigned URL y cinco de margen); antes responde 409 porque el enlace todavía podría producir una subida/evento S3. Los jobs `COMPLETED` y `FAILED` sí se pueden eliminar.

`POST /jobs/{job_id}/ask` acepta `{ "question": "..." }` y devuelve `202` con `ask_id`; el estado se consulta en `GET /jobs/{job_id}/asks/{ask_id}`. Ambas rutas usan el Cognito Authorizer existente. La identidad se toma del claim `sub`, el job debe pertenecer al usuario y estar `COMPLETED`, y el worker vuelve a verificarlo antes de leer el resultado y al persistir la respuesta. Un job de otro usuario o ya eliminado devuelve 404. No se acepta `user_id` del cliente.

Las preguntas usan `QuestionQueue` y `QuestionDeadLetterQueue`, sin mezclar notificaciones S3. La Lambda de preguntas tiene concurrencia reservada de 1, un timeout de 900 segundos y un timeout máximo de lectura HTTP Ollama de 780 segundos; en ejecución el worker lo reduce si el tiempo restante de Lambda exige más margen para persistir el estado. API Gateway devuelve enseguida y no mantiene una conexión durante la inferencia. El event source mapping no fija `MaximumConcurrency`: AWS exige un mínimo de 2 para ese ajuste, mientras la concurrencia reservada de la función mantiene el máximo real de invocaciones en 1. Con más demanda, vigila throttles y edad del mensaje. Los fallos transitorios que el handler alcanza a capturar programan una visibilidad por mensaje con backoff (30, 60 segundos, limitada a 300); la visibilidad configurada de la cola sigue siendo 5.400 segundos para cubrir una ejecución Lambda completa. Un cierre abrupto no puede reducir esa visibilidad y puede tardar hasta 90 minutos en reintentarse. El token Ollama se obtiene en tiempo de ejecución desde el mismo secreto de Secrets Manager cuando `ProcessorDeployment=fargate`; no se envía al navegador ni se configura como texto plano. Para operar con `ProcessorDeployment=local`, la Lambda local no requiere token y `deploy-local.sh` apunta Ollama al host local.

Al exceder los tres receives, `QuestionDeadLetterQueue` activa una Lambda reconciliadora de mínimo privilegio. Esta marca `FAILED` consultas que aún estén `PENDING`/`PROCESSING`, sin tocar estados terminales ni consultas expiradas. Así se cubren también invocaciones terminadas por timeout o caída antes de actualizar DynamoDB. Si la reconciliación no puede actualizar DynamoDB por un fallo transitorio, devuelve ese mensaje como fallo parcial para reintentar desde la DLQ; además, el endpoint de estado convierte una consulta sin cambios durante 9 horas a `FAILED` de forma condicional. Los registros dejan de ser accesibles al cumplirse su TTL lógico de 24 horas, aunque DynamoDB elimine físicamente el item más tarde.

Los parámetros SAM de esta función son `OllamaAskNumPredict=1000`, `OllamaAskReadTimeout=780`, `OllamaAskContextMaxChars=16000`, `QuestionQueueVisibilityTimeout=5400`, `QuestionRetryDelaySeconds=30` y `MaxUserQuestionsPerDay=50`. La Lambda no declara `VpcConfig`; por la configuración del código SAM no hereda el security group de Fargate. Una Lambda no conectada a una VPC tiene egreso público administrado por AWS, pero no una IP de salida fija. Por tanto la ruta pública HTTPS, DNS, firewall del host y Apache deben permitir la solicitud desde ese origen. El `OllamaIPv4Cidr` solo configura el egreso del security group de Fargate; no configura Apache ni el firewall del servidor. La configuración del repositorio no demuestra que la Lambda desplegada pueda conectarse; eso debe comprobarse en un despliegue controlado mirando categorías/status en CloudWatch y los logs de Apache, sin habilitar logging del token o del contenido.

TLS se verifica mediante el comportamiento predeterminado de `requests`; el cliente no desactiva la validación de certificados. El secreto se obtiene desde Secrets Manager en tiempo de ejecución y el header Bearer no se registra. `QuestionProcessorRole` solo lee `results/*`, el secreto exacto, las dos tablas y `QuestionQueue`; no tiene acceso al PDF original. El reconciliador separado solo lee/borra/renueva mensajes de la DLQ, actualiza `QuestionTable` y escribe en su log group.

El resultado nuevo en `ResultsBucket` agrega `page_extractions` con transcripción, notas y elementos visuales de cada página, generados y validados durante el procesamiento inicial. Resultados históricos se leen sin migración: las versiones estructuradas se dividen usando los encabezados `--- Página N ---`; los resultados legacy usan `page_results`. Si no existen límites de página verificables, la respuesta no inventa una cita.

La recuperación inicial usa coincidencias léxicas por páginas/fragmentos, no embeddings. Conserva el resumen global y selecciona extractos completos hasta `OllamaAskContextMaxChars`; no recorta un extracto por la mitad. Si se omitió parte del documento, la API marca `context_limited` y la interfaz lo indica. La respuesta debe decir que no encontró la información cuando la evidencia enviada no basta. Esto reduce contexto, pero puede omitir un pasaje cuya redacción no coincida con la pregunta.

Las preguntas están limitadas a 2.000 caracteres y 50 por usuario al día, y sus registros (pregunta y respuesta) tienen TTL de un día. DynamoDB TTL es asíncrono; la API deja de exponer la consulta al vencer `expires_at`, y `DELETE /jobs/{job_id}` elimina explícitamente las consultas conocidas antes de borrar el job. El historial visual de preguntas/respuestas solo vive en el estado React y no se restaura al recargar. React deja de consultar después de 20 minutos para no dejar polling infinito; conserva `PENDING`/`PROCESSING` y ofrece **Revisar estado**, en lugar de afirmar que el backend terminó en `FAILED` por un timeout local.

## document-processor en AWS

La imagen ejecuta FastAPI y hasta dos consumidores SQS en la misma task ECS/Fargate. Cada slot tiene su propio long poll, receipt handle, lease, heartbeat y estado de job. Los clientes boto3 de servicio se comparten donde son thread-safe; DynamoDB resource se crea por hilo. `document_service.process_pdf(...)` invoca páginas en secuencia dentro de cada job y mantiene un `context_summary` local a ese documento. `/process` y Cloud Map se conservan para diagnóstico/local y rollback, pero no forman parte del camino productivo. ECS registra las IP privadas de sus tasks en Cloud Map y `/health` comprueba que todos los consumidores configurados sigan vivos; una inferencia larga no vuelve unhealthy al servicio, pero un consumidor terminado devuelve HTTP 503 para que ECS reemplace la task.

`ProcessorMaxConcurrentJobs` controla el máximo por task y admite `1` o `2`; el default sigue siendo `1` para que el aumento se active explícitamente. Con `DocumentProcessorDesiredCount=1` y `ProcessorMaxConcurrentJobs=2`, habrá como máximo dos jobs activos en la task. Si más adelante se eleva el desired count, la capacidad agregada será `desiredCount × ProcessorMaxConcurrentJobs`. El CPU/memoria de task se mantiene en 0.25 vCPU/1 GiB: como Ollama corre fuera de Fargate y se rasteriza una página por job a la vez, la memoria adicional esperada es principalmente hasta dos PDFs/imágenes en proceso; no se aumentó sin mediciones de carga concurrente. Conviene revisar métricas ECS después de activar los dos slots.

El servidor local inspeccionado corre Ollama `0.40.2`; no tiene `OLLAMA_NUM_PARALLEL` configurado, cuyo default documentado es `1`. Por tanto, dos jobs Fargate enviarán solicitudes HTTP simultáneas, pero no se debe asumir que Ollama inferirá ambas a la vez. La documentación/código actual de Ollama indica que ciertas arquitecturas, incluida `qwen3vl`, pueden forzarse a un slot por limitaciones del modelo; además la memoria requerida escala con `OLLAMA_NUM_PARALLEL × OLLAMA_CONTEXT_LENGTH` ([FAQ de Ollama](https://github.com/ollama/ollama/blob/main/docs/faq.mdx), [programador de Ollama](https://github.com/ollama/ollama/blob/main/server/sched.go)). No recomendamos habilitarlo aún: la GTX 1070 tiene 8 GiB y el contexto está en 16.384; hay que verificar la memoria con el modelo cargado antes de probar dos slots.

Para probar dos slots en el host Ollama solo después de validar memoria, añade el override sin reemplazar otros valores del servicio:

```bash
sudo systemctl edit ollama
# En el editor, dentro de [Service]:
# Environment="OLLAMA_NUM_PARALLEL=2"
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

El override solo solicita el valor; no confirma dos slots efectivos para `qwen3-vl`. Antes de asumir concurrencia, revisa el journal por el aviso `model architecture does not currently support parallel requests` y observa el runner con `ollama ps` durante una prueba controlada posterior. Para revertir, vuelve a `sudo systemctl edit ollama`, elimina únicamente la línea de `OLLAMA_NUM_PARALLEL` agregada, conserva `OLLAMA_HOST=127.0.0.1:11434` y cualquier otro override existente, luego ejecuta los mismos `daemon-reload` y `restart`. No uses `systemctl revert`, porque podría eliminar overrides ajenos a este cambio.

En SIGTERM, el worker deja de long-pollear y espera hasta 45 segundos para que sus consumidores terminen ordenadamente, dentro del `StopTimeout=60` de ECS. Durante esa espera los jobs activos conservan heartbeat y lease. Si una inferencia tarda más, la task puede terminar antes de completar: no confirma el mensaje, conserva checkpoints ya confirmados y SQS lo vuelve a entregar cuando vence la visibilidad; la nueva task reanuda desde checkpoints. Un mensaje recibido justo al comenzar el apagado tampoco se procesa ni se confirma y puede permanecer invisible hasta vencer su ventana de visibilidad.

La task Fargate está preparada en una subnet pública con asignación de IP pública y ruta al Internet Gateway para descargar la imagen de ECR, enviar logs, hacer long polling de SQS y llamar al proxy HTTPS de Ollama. AWS requiere una IP pública en la task para el egress directo por IGW; la Lambda no recibe IP pública por estar en la subnet pública ([networking Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-networking.html), [IGW y direcciones públicas](https://docs.aws.amazon.com/vpc/latest/userguide/VPC_Internet_Gateway.html)). La IP pública no implica que el puerto 8000 quede abierto: no hay ingress público. El security group mantiene TCP 11443 únicamente a `OllamaIPv4Cidr` (`99.35.202.200/32`). La regla separada TCP 443 a `0.0.0.0/0` permite a Fargate alcanzar SQS y los endpoints públicos de ECR, CloudWatch Logs y Secrets Manager; no abre el puerto Ollama a Internet. S3 y DynamoDB usan los endpoints gateway existentes. No se crea NAT Gateway ni ALB. La DNS privada de Cloud Map apunta a la IP privada de la task ([service discovery](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-discovery.html)). Se usa una sola subnet/AZ y una task por costo; esto no ofrece alta disponibilidad ante la caída de esa AZ.

`ProcessorDeployment=fargate` es el valor AWS predeterminado. `deploy-local.sh` lo cambia a `local` y continúa usando `DocumentProcessorUrl`; ese modo omite ECR, ECS y VPC, preservando LocalStack y Compose. En local, Compose sigue publicando processor en 8000 y apunta a Ollama del host por 11434. La autenticación Cognito sigue sin bypass y requiere AWS real con la licencia LocalStack actual.

El servicio ECS inicia con `DocumentProcessorDesiredCount=0`. El primer despliegue crea el repositorio y la infraestructura; después se construye y sube la imagen; por último se actualiza a `1`. Esto evita intentar iniciar una imagen que todavía no existe en ECR.

Parámetros principales: `OllamaBaseUrl` (`https://la-ea.duckdns.org:11443`, endpoint probado con certificado TLS válido), `OllamaIPv4Cidr` (`99.35.202.200/32`), `DocumentProcessorImageTag` (`latest`), `DocumentProcessorDesiredCount` (`0`), `VpcCidr` (`10.40.0.0/16`) y `PublicSubnetCidr` (`10.40.1.0/24`). Cambia los CIDR de VPC/subnet si se solapan con otra red.

### Costos permanentes

- Una task Fargate de 0.25 vCPU/1 GiB encendida todo el mes cuesta aproximadamente US$10.60/mes en `us-east-2`, antes de logs y transferencia; la tarifa depende de región y recursos asignados ([precios Fargate](https://aws.amazon.com/fargate/pricing/)).
- La IPv4 pública cuesta US$0.005/hora, unos US$3.65 por mes continuo; ECR cobra almacenamiento, con lifecycle de cinco imágenes. CloudWatch Logs conserva 14 días y cobra ingestión/almacenamiento ([precios VPC/IPv4](https://aws.amazon.com/vpc/pricing/)).
- Cloud Map crea una private hosted zone (~US$0.50/mes) y cobra por task registrada (~US$0.10 por recurso-mes). Los gateway endpoints de S3 y DynamoDB no tienen cargo por hora ([precios Cloud Map](https://aws.amazon.com/cloud-map/pricing/), [Route 53](https://aws.amazon.com/route53/pricing/), [gateway endpoints](https://docs.aws.amazon.com/vpc/latest/privatelink/vpc-endpoints-ddb.html)).
- Con una task encendida, el costo base indicativo ronda US$14.88/mes antes de CloudWatch, ECR, transferencia y llamadas a Ollama. Mientras `desiredCount=0`, no se cobra Fargate ni una IP pública de task, pero sí quedan la hosted zone y almacenamiento ECR; la VPC, subnets, rutas y gateway endpoints no tienen cargo fijo.
- No se añade ALB, NAT Gateway ni interface endpoint, que sumarían costos permanentes. Secrets Manager sí aplica su cargo mensual por secreto y cargos de solicitudes; el secret declarado por la plantilla no contiene un valor hasta que lo cargues.
- La reconciliación de preguntas agrega una Lambda de 128 MB/30 s, un log group retenido 7 días y un event source mapping sobre la DLQ. Solo invoca al llegar mensajes agotados; el costo es por invocación/SQS/CloudWatch y no mantiene cómputo encendido. La Lambda principal de preguntas sigue generando cargos por duración de inferencia y por cada invocación.

### Proxy remoto de Ollama: TLS y Bearer token

`document-processor` usa el cliente `requests` existente para `POST /api/chat` con `OLLAMA_BASE_URL=https://la-ea.duckdns.org:11443`. Requests mantiene habilitada su verificación TLS normal; no se configura `verify=False` ni CA personalizada. Si `OLLAMA_API_TOKEN` no está definida o está vacía (Compose local), no manda Authorization; cuando está presente, envía `Authorization: Bearer …`. Los logs y las excepciones del cliente no registran el header, el valor del token ni el texto de los errores HTTP. La petición actual usa `stream=false`; Apache proxya la respuesta normal de Ollama y el timeout permite respuestas largas. No se ha activado streaming en el cliente.

El endpoint real, ya configurado y probado fuera del repositorio, sigue la ruta `https://la-ea.duckdns.org:11443` → Apache/TLS/Bearer → `http://127.0.0.1:11434`. Las pruebas externas confirmaron HTTP 403 sin token o con token incorrecto, y HTTP 200 con el token correcto. La referencia Apache versionada está en [`infrastructure/apache/ollama-proxy.conf.example`](infrastructure/apache/ollama-proxy.conf.example); no contiene secretos ni certificados privados. La configuración del host, Apache y Ollama queda fuera del despliegue de este repositorio.

Ollama permanece escuchando exclusivamente en `127.0.0.1:11434`; AWS no conecta directamente a ese puerto. En el router, el acceso público TCP 11443 termina en Apache y no debe existir un forward público al 11434. Apache, Ollama, el router y UFW no son modificados por este repositorio.

El DNS `la-ea.duckdns.org` y el certificado público de Let's Encrypt ya están configurados y validados. `OllamaBaseUrl` usa ese hostname para que TLS valide el nombre del certificado, sin CA personalizada ni bypass de validación.

La Task Definition obtiene `OLLAMA_API_TOKEN` del recurso Secrets Manager referenciado por `ValueFrom`; no es una variable de entorno plaintext en CloudFormation. El único permiso para leerlo es `secretsmanager:GetSecretValue` sobre el ARN exacto y corresponde al task execution role. Después del primer despliegue con desired count 0, carga manualmente el token ya existente en el secreto mediante la consola de Secrets Manager antes de iniciar tasks. No se genera otro token ni se incluye su valor en el repositorio o en comandos. ECS inyecta el secreto al iniciar cada task; tras una rotación hay que reiniciar/desplegar tasks ([documentación ECS](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html)).

### Comandos para despliegue posterior

No ejecutar durante esta iteración. Cuando autorices el despliegue, primero autentica el CLI con IAM Identity Center:

```bash
aws configure sso --profile document-ai-ohio
aws sso login --profile document-ai-ohio
export AWS_PROFILE=document-ai-ohio
export AWS_REGION=us-east-2
aws sts get-caller-identity
```

Construye y despliega SAM con el servicio ECS en cero. Ejecuta el ensamblador antes de cada build si cambias los módulos:

```bash
python3 infrastructure/aws/assemble_template.py
sam build --template-file infrastructure/aws/.template-assembled.yml
sam deploy \
  --template-file .aws-sam/build/template.yaml \
  --config-file infrastructure/aws/samconfig.toml \
  --parameter-overrides \
    Environment=dev ProcessorDeployment=fargate DocumentProcessorDesiredCount=0 \
    OllamaModel=qwen3-vl:8b-instruct \
    OllamaBaseUrl=https://la-ea.duckdns.org:11443 OllamaIPv4Cidr=99.35.202.200/32 \
    QueueVisibilityTimeout=900 FrontendOrigin=https://dfw9n3vcykvho.cloudfront.net
```

Obtén el URI de ECR, autentica Docker y sube la imagen construida desde el Dockerfile existente:

```bash
ECR_URI=$(aws cloudformation describe-stacks \
  --stack-name document-ai-pipeline \
  --query "Stacks[0].Outputs[?OutputKey=='DocumentProcessorRepositoryUri'].OutputValue" \
  --output text)
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${ECR_URI%%/*}"
docker build --platform linux/amd64 -t "$ECR_URI:latest" \
  -f document-processor/Dockerfile document-processor
docker push "$ECR_URI:latest"
```

Vuelve a desplegar conservando los mismos overrides y estableciendo `DocumentProcessorDesiredCount=1`:

```bash
sam deploy \
  --template-file .aws-sam/build/template.yaml \
  --config-file infrastructure/aws/samconfig.toml \
  --parameter-overrides \
    Environment=dev ProcessorDeployment=fargate DocumentProcessorDesiredCount=1 \
    OllamaModel=qwen3-vl:8b-instruct \
    OllamaBaseUrl=https://la-ea.duckdns.org:11443 OllamaIPv4Cidr=99.35.202.200/32 \
    QueueVisibilityTimeout=900 FrontendOrigin=https://dfw9n3vcykvho.cloudfront.net
```

Para cada versión posterior de la imagen, usa un tag nuevo, súbelo a ECR y pasa ese valor como `DocumentProcessorImageTag` en el despliegue para registrar una nueva revisión de Task Definition. Luego revisa Outputs y estado ECS:

```bash
aws cloudformation describe-stacks --stack-name document-ai-pipeline \
  --query 'Stacks[0].Outputs' --output table
CLUSTER_NAME=$(aws cloudformation describe-stacks --stack-name document-ai-pipeline \
  --query "Stacks[0].Outputs[?OutputKey=='DocumentProcessorClusterName'].OutputValue" \
  --output text)
SERVICE_NAME=$(aws cloudformation describe-stacks --stack-name document-ai-pipeline \
  --query "Stacks[0].Outputs[?OutputKey=='DocumentProcessorServiceName'].OutputValue" \
  --output text)
aws ecs describe-services --cluster "$CLUSTER_NAME" \
  --services "$SERVICE_NAME" --query 'services[0].deployments'
```

Finalmente configura `JobsApiUrl`, `CognitoUserPoolId` y `CognitoUserPoolClientId` en `frontend/.env.local` como `VITE_API_BASE_URL`, `VITE_COGNITO_USER_POOL_ID` y `VITE_COGNITO_USER_POOL_CLIENT_ID`. No configures `VITE_COGNITO_ENDPOINT` al usar AWS.

Pruebas unitarias de API/worker:

```bash
cd worker
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

LocalStack usa cinco pollers para SQS Standard por defecto. Mientras Lambda ejecuta A con concurrencia reservada `1`, los demás pollers pueden recibir B/C, ser throttled y dejar temporalmente esos mensajes invisibles durante el visibility timeout. El perfil local limita el event source mapping a un poller, por lo que los mensajes posteriores permanecen visibles esperando turno. Este ajuste afecta solo al emulador; no cambia la concurrencia de Lambda ni la arquitectura AWS. En AWS, Lambda/SQS conserva los mensajes tras throttling y los reintenta según visibilidad y backoff del event source mapping.

## Pruebas

Las pruebas usan PDFs generados en memoria y un cliente Ollama simulado; no necesitan Ollama real:

```bash
cd document-processor
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

La suite verifica `/health`, PDF de una y varias páginas, contexto secuencial, `user_prompt` vacío/definido, error Ollama, PDF inválido, tamaño máximo y estructura final. Prueba el cliente Ollama manualmente con:

```bash
curl http://localhost:11434/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-vl:8b-instruct","stream":false,"messages":[{"role":"user","content":"Responde en español: ¿estás disponible?"}]}'
```

## Prueba end-to-end

1. Levanta Ollama, descarga el modelo y arranca `docker compose up --build -d`.
2. En LocalStack usa `bash infrastructure/aws/deploy-local.sh` y el processor local; en AWS sigue los pasos de “Comandos para despliegue posterior”.
3. Copia `JobsApiUrl`, `CognitoUserPoolId` y `CognitoUserPoolClientId` desde los outputs del stack a `frontend/.env.local` con los nombres `VITE_API_BASE_URL`, `VITE_COGNITO_USER_POOL_ID` y `VITE_COGNITO_USER_POOL_CLIENT_ID`; reinicia Vite.
4. Registra una cuenta, confirma el código enviado por Cognito e inicia sesión.
5. Selecciona un PDF multipágina, agrega opcionalmente una instrucción y pulsa **Cargar y analizar**.
6. Confirma el recorrido `UPLOAD_PENDING → QUEUED → PROCESSING → COMPLETED` y revisa consolidación y análisis por página. Un error de provider aparece como `FAILED` después de los retries SQS.

Logs en AWS:

```bash
aws logs tail /ecs/document-ai-pipeline-dev-document-processor --follow
```

Los logs del consumidor y de FastAPI comparten este log group; los logs locales del processor se ven con `docker compose logs -f document-processor`.

## Dependencias agregadas

- Processor: `PyMuPDF`, `requests`, `python-dotenv` (además de FastAPI, Uvicorn y multipart).
- Frontend: React 18, TypeScript, Vite y plugin React.
- Tests: pytest y httpx2 como dependencias de desarrollo del processor (lo requiere el `TestClient` de las versiones actuales de Starlette).
- Worker conserva `boto3` y `requests`; AWS SDK integra S3/DynamoDB.

## Límites actuales

- Cada usuario puede conservar hasta 20 jobs/resultados. La cuota se reserva atómicamente en DynamoDB y se libera al eliminar el job. Jobs y resultados permanecen hasta su eliminación explícita; el PDF original se elimina tras completar el análisis y `DocumentsBucket` expira objetos restantes a los 30 días. Los resultados no tienen lifecycle corto y el almacenamiento crece si los usuarios no eliminan jobs.
- El documento ya no ocupa una Lambda mientras Ollama trabaja. Cada página tiene `OLLAMA_READ_TIMEOUT=1200` por defecto y la visibilidad SQS se renueva durante todo el PDF. PDFs grandes o muchas páginas aún pueden exceder memoria o contexto del modelo; no hay todavía límite de páginas ni resumen jerárquico.
- El análisis depende de que `qwen3-vl:8b-instruct` quepa en la memoria disponible del host. El processor conserva cada análisis de página y envía todos a la consolidación.
- El processor no valida antivirus ni inspecciona contenido malicioso, según el alcance actual.
