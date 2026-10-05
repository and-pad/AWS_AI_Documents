# AWS AI Documents

Pipeline asíncrono para analizar PDFs por página con `qwen3-vl:8b` servido por Ollama.

## Estructura relevante

```text
frontend/                         React + Vite: alta de trabajos, carga y polling
document-processor/app/
  main.py                         FastAPI /health y /process
  document_service.py             PDF → imágenes → análisis secuencial → consolidación
  ollama_client.py                Cliente HTTP aislado para Ollama
  settings.py                     Variables de entorno y límites
worker/app/
  jobs.py                         API de alta/consulta y URLs S3 prefirmadas
  worker.py                       Consumidor SQS: S3 → processor → estado/resultado
  ai_client.py                    HTTP multipart hacia document-processor
infrastructure/aws/template.yml   Buckets S3, SQS/DLQ, Lambda, API Gateway, DynamoDB
compose.yaml                      document-processor local
```

## Flujo y decisiones

1. React hace `POST /jobs` con nombre, tamaño, tipo y `user_prompt`. La Lambda de API valida el PDF/tamaño, crea un ID y guarda el trabajo `UPLOAD_PENDING` en DynamoDB; devuelve una presigned PUT URL de corta duración.
2. React sube directamente a S3 sin credenciales AWS. La presigned URL exige tipo/tamaño y metadata `x-amz-meta-job-id`. Tras un PUT exitoso, React comienza polling. JobsApi comprueba el objeto y su metadata en el primer `GET /jobs/{job_id}` y marca el trabajo `QUEUED`; así un job ya subido no permanece etiquetado `UPLOAD_PENDING` mientras espera turno en SQS.
3. S3 → SQS → Worker conserva el orquestador existente y sus valores `BatchSize: 1` y `ReservedConcurrentExecutions: 1`. SQS sigue siendo el disparador del procesamiento; el polling solo refleja que el PUT terminó y no envía mensajes ni decide cuándo ejecutar. El worker marca `PROCESSING`, usa `job_id` para leer `user_prompt` desde DynamoDB y lo envía como parte multipart. Así el prompt se asocia al objeto sin meter texto extenso en metadata limitada de S3 ni alterar el evento nativo de S3.
4. `document-processor` abre el PDF con PyMuPDF, rasteriza una página cada vez, envía la imagen a Ollama y produce el análisis página a página. Al pedir cada página, incluye un `context_summary` limitado a `CONTEXT_MAX_CHARS`; el modelo lo actualiza con entidades, datos del documento y continuidad relevantes. Los análisis detallados no se reutilizan como contexto y se conservan completos para la consolidación final.
5. El worker guarda el resultado JSON en `ResultsBucket` y marca `COMPLETED` en DynamoDB. La API `GET /jobs/{job_id}` lee ese resultado. Fallos de processor no se convierten en éxito: se informa el mensaje fallido a SQS para retry/DLQ; al tercer intento el registro queda `FAILED`.

El bucket de resultados está separado del bucket de entrada: este último envía eventos S3 a SQS, por lo que guardar el resultado en el mismo bucket volvería a activar el worker. Los trabajos vencen mediante TTL de DynamoDB a los 30 días; los resultados tienen lifecycle S3 de 30 días. Los PDFs de entrada conservan la retención que ya tenía el bucket.

## Ollama y document-processor local

Requiere Docker Compose, Ollama en el host y el modelo descargado. En el host, permite conexiones desde el contenedor (el valor por defecto de Ollama suele escuchar solo en loopback):

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
ollama pull qwen3-vl:8b
curl http://localhost:11434/api/tags
```

En otra terminal, desde la raíz:

```bash
cp .env.example .env
docker compose up --build -d
curl http://localhost:8000/health
docker compose logs -f document-processor
```

En Docker Desktop `host.docker.internal` suele estar disponible. En Linux Compose añade `host.docker.internal:host-gateway`, así que `OLLAMA_URL=http://host.docker.internal:11434` apunta al host sin fijar una IP. En otro entorno basta cambiar `OLLAMA_URL`; no se instala Ollama dentro del contenedor.

Las variables disponibles son:

| Variable | Predeterminado | Uso |
| --- | --- | --- |
| `OLLAMA_URL` | `http://localhost:11434` en proceso directo; Compose lo establece a `http://host.docker.internal:11434` | Endpoint del proveedor |
| `OLLAMA_MODEL` | `qwen3-vl:8b` | Modelo Ollama |
| `CORS_ORIGINS` | `http://localhost:5173` | Orígenes permitidos por FastAPI |
| `MAX_UPLOAD_BYTES` | `20971520` | Máximo 20 MiB |
| `PDF_RENDER_DPI` | `180` | Resolución de render de páginas |
| `CONTEXT_MAX_CHARS` | `3000` | Límite del contexto resumido |

El endpoint multipart es `POST /process` con campos `file` y `user_prompt` (opcional). `/health` no llama a Ollama. Los errores del proveedor se registran y `/process` contesta 502 sin stack trace.

## Frontend

Instala dependencias una vez y configura la URL base de API que muestra el output de SAM:

```bash
cd frontend
npm install
cp .env.example .env.local
# edita VITE_API_BASE_URL para que apunte al output JobsApiUrl
npm run dev
```

Abre `http://localhost:5173`. El navegador solo conoce la URL API y la URL temporal prefirmada; no recibe claves AWS. Muestra el estado retornado por backend y consulta cada 2.5 segundos hasta `COMPLETED` o `FAILED`.

El formulario permite enviar otro PDF en cuanto termina el PUT anterior, sin esperar a que concluya el análisis. Mantiene una lista/polling independiente por job para que A, B y C puedan estar visibles a la vez.

## SAM y despliegue

Desde la raíz del repositorio:

```bash
bash infrastructure/aws/deploy-local.sh
```

El script obtiene el gateway de la red Docker `bridge` y lo configura como `DocumentProcessorUrl`; LocalStack ejecuta Lambda en esa red y el puerto 8000 del processor está publicado en el host. Esto evita depender de `host.docker.internal`, que el runtime Lambda de este entorno no resuelve. También reaplica `ReservedConcurrentExecutions=1`: el despliegue SAM de LocalStack dejó ese valor sin definir en la configuración activa de Lambda pese a estar presente en el template. Una Lambda desplegada en AWS no puede acceder al host del desarrollador: en AWS se debe pasar la URL privada/pública alcanzable de un servicio desplegado (por ejemplo ECS/Fargate). La URL del processor es un parámetro, no una constante del código.

Si el wrapper `lstk` no carga credenciales/configuración de AWS por sí solo, configura primero el perfil/endpoints que usa ese wrapper. Los valores SQS existentes permanecen `BatchSize: 1` y concurrencia reservada `1`.

Para LocalStack, arráncalo o reinícialo con el perfil del repositorio:

```bash
lstk --config infrastructure/aws/lstk-local.toml start
# si ya está iniciado:
lstk --config infrastructure/aws/lstk-local.toml restart
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
  -d '{"model":"qwen3-vl:8b","stream":false,"messages":[{"role":"user","content":"Responde en español: ¿estás disponible?"}]}'
```

## Prueba end-to-end

1. Levanta Ollama, descarga el modelo y arranca `docker compose up --build -d`.
2. Construye/despliega la infraestructura con el processor accesible desde Lambda.
3. Copia `JobsApiUrl` desde los outputs del stack a `frontend/.env.local` como `VITE_API_BASE_URL` y reinicia Vite.
4. Selecciona un PDF multipágina, agrega opcionalmente una instrucción y pulsa **Cargar y analizar**.
5. Confirma el recorrido `UPLOAD_PENDING → QUEUED → PROCESSING → COMPLETED` y revisa consolidación y análisis por página. Un error de provider aparece como `FAILED` después de los retries SQS.

Logs en AWS:

```bash
aws logs tail /aws/lambda/<nombre-físico-de-DocumentProcessorFunction> --follow
```

El nombre físico también aparece en la consola CloudFormation/Lambda. Los logs locales del processor se ven con `docker compose logs -f document-processor`.

## Dependencias agregadas

- Processor: `PyMuPDF`, `requests`, `python-dotenv` (además de FastAPI, Uvicorn y multipart).
- Frontend: React 18, TypeScript, Vite y plugin React.
- Tests: pytest y httpx como dependencias de desarrollo del processor.
- Worker conserva `boto3` y `requests`; AWS SDK integra S3/DynamoDB.

## Límites actuales

- El resultado se consulta durante 30 días mediante TTL de DynamoDB y lifecycle del bucket; el TTL elimina la fila de forma eventual. Los PDFs de entrada conservan la retención vigente del bucket.
- No hay autenticación de usuario en API Gateway; cualquier cliente que conozca el endpoint puede crear trabajos. Para exponerlo fuera del entorno de pruebas, se debe integrar autenticación/autorización.
- Lambda mantiene el timeout existente de 900 segundos. PDFs grandes o muchas páginas pueden exceder el tiempo, la memoria o el contexto del modelo; no hay aún límite de número de páginas ni resumen jerárquico para la consolidación.
- El análisis depende de que `qwen3-vl:8b` quepa en la memoria disponible del host. El processor conserva cada análisis de página y envía todos a la consolidación.
- El processor no valida antivirus ni inspecciona contenido malicioso, según el alcance actual.
