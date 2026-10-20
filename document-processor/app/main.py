import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.document_service import process_pdf
from app.ollama_client import OllamaError
from app.settings import get_settings
from app.sqs_worker import SqsDocumentWorker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Uvicorn is intentionally started without --workers. This creates exactly
    # one long-poll consumer per ECS task while the API event loop stays free
    # for health checks and local diagnostic requests.
    worker = SqsDocumentWorker()
    worker.start()
    app.state.sqs_worker = worker
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title="document-processor", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings["cors_origins"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/process")
async def process_document(file: UploadFile = File(...), user_prompt: str | None = Form(default=None)):
    max_bytes = get_settings()["max_upload_bytes"]
    contents = await file.read(max_bytes + 1)
    if not contents:
        raise HTTPException(status_code=400, detail="El PDF está vacío")
    if len(contents) > max_bytes:
        raise HTTPException(status_code=413, detail=f"El archivo supera el límite de {max_bytes} bytes")
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF")
    try:
        return await run_in_threadpool(process_pdf, file.filename or "document.pdf", contents, user_prompt, None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OllamaError:
        logger.error("Falló Ollama para filename=%s", file.filename)
        raise HTTPException(status_code=502, detail="El proveedor de análisis documental no está disponible") from None
    finally:
        contents = b""
