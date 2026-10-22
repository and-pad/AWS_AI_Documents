import json
import logging

import fitz
import pytest
from fastapi.testclient import TestClient

from app import main
from app.document_service import (
    MAX_STRUCTURED_OUTPUT_ATTEMPTS,
    StructuredOutputError,
    _generate_validated,
    _validate_model_output,
    process_pdf,
)
from app.ollama_client import OllamaError
from app.ollama_client import OllamaClient, OllamaOutput
from app.settings import get_settings
from app.checkpoints import S3PageCheckpointStore
import requests


class FakeHttpResponse:
    def __init__(self, payload, status_code=200, content_type="application/json; charset=utf-8"):
        self.payload = payload
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeOllama:
    def __init__(self, fail_on_call=None, responses=None):
        self.calls = []
        self.formats = []
        self.options = []
        self.fail_on_call = fail_on_call
        self.responses = list(responses or [])

    def chat(self, messages, image=None, format=None, options=None):
        self.calls.append((messages, image))
        self.formats.append(format)
        self.options.append(options)
        if self.fail_on_call == len(self.calls):
            raise OllamaError("provider unavailable")
        if self.responses:
            return self.responses.pop(0)
        if image is None:
            return json.dumps({
                "analysis": {
                    "document_type": "Documento de prueba",
                    "summary": "Resumen consolidado.",
                    "key_points": ["Dato relevante"],
                },
                "visual_elements": {"stamps": [], "signatures": [], "other_marks": []},
                "requested_output": {"response": "Respuesta simulada"},
            }, ensure_ascii=False)
        page_number = len([call for call in self.calls if call[1] is not None])
        return json.dumps({
            "transcription": f"Encabezado {page_number}\nLínea literal {page_number}",
            "analysis_notes": f"Análisis {page_number}",
            "visual_elements": {"stamps": [], "signatures": [], "other_marks": []},
            "context_summary": f"Documento; página previa {page_number}",
        })


def make_pdf(page_count=1, label="Contenido visible"):
    document = fitz.open()
    for page_number in range(page_count):
        page = document.new_page()
        page.insert_text((72, 72), f"{label} {page_number + 1}")
    contents = document.tobytes()
    document.close()
    return contents


class MemoryPageCheckpointStore:
    def __init__(self, job_id="job-checkpoint", processing_owner="owner-a"):
        self.job_id = job_id
        self.processing_owner = processing_owner
        self.prefix = f"checkpoints/{job_id}/"
        self.manifest_key = None
        self.objects = {}
        self.sequence = 0
        self.fail_save = False
        self.manifest = None

    def load_manifest(self):
        return self.manifest

    def load_checkpoint(self, entry):
        if not isinstance(entry, dict) or not entry.get("key"):
            raise ValueError("missing checkpoint entry")
        key = entry["key"]
        checkpoint = self.objects[key]
        if isinstance(checkpoint, Exception):
            raise checkpoint
        raw = json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        import hashlib
        if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            raise ValueError("checkpoint checksum mismatch")
        return checkpoint, key

    def list_objects(self):
        return [{"Key": key} for key in self.objects]

    def load(self, key):
        value = self.objects[key]
        if isinstance(value, Exception):
            raise value
        return value

    def save(self, page_number, checkpoint):
        if self.fail_save:
            raise RuntimeError("checkpoint write failed")
        self.sequence += 1
        key = f"{self.prefix}page-{page_number:04d}/{self.processing_owner}-{self.sequence}.json"
        self.objects[key] = checkpoint
        import hashlib
        raw = json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        pages = {
            number: entry for number, entry in (self.manifest or {}).get("pages", {}).items()
            if int(number) < page_number
        }
        pages[str(page_number)] = {"key": key, "sha256": hashlib.sha256(raw).hexdigest()}
        self.manifest = {
            "manifest_version": 1,
            "job_id": checkpoint["job_id"],
            "pdf_sha256": checkpoint["pdf_sha256"],
            "total_pages": checkpoint["total_pages"],
            "schema_version": checkpoint["schema_version"],
            "pipeline_fingerprint": checkpoint["pipeline_fingerprint"],
            "pages": pages,
        }
        self.manifest_key = f"{self.prefix}manifest/{self.processing_owner}-{self.sequence}.json"
        return key, len(json.dumps(checkpoint))

    def delete_all(self):
        count = len(self.objects)
        self.objects.clear()
        return count


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "2000000")
    monkeypatch.setenv("PDF_RENDER_DPI", "72")
    monkeypatch.setenv("OLLAMA_PAGE_NUM_PREDICT", "6000")
    monkeypatch.setenv("OLLAMA_SYNTHESIS_NUM_PREDICT", "2000")
    monkeypatch.setenv("OLLAMA_READ_TIMEOUT", "1800")
    yield
    get_settings.cache_clear()


def test_health():
    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_returns_503_when_a_configured_consumer_died(monkeypatch):
    class DeadConsumerWorker:
        def __init__(self):
            self.health_snapshot = {
                "status": "unhealthy",
                "sqs_worker": {
                    "enabled": True,
                    "expected_consumers": 2,
                    "healthy_consumers": 1,
                    "dead_worker_slots": [2],
                    "active_jobs": 1,
                },
            }

        def start(self):
            return None

        def stop(self, **_kwargs):
            return None

    monkeypatch.setattr(main, "SqsDocumentWorker", DeadConsumerWorker)
    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["sqs_worker"]["dead_worker_slots"] == [2]


def test_health_remains_200_while_a_job_is_active(monkeypatch):
    class BusyWorker:
        def __init__(self):
            self.health_snapshot = {
                "status": "ok",
                "sqs_worker": {
                    "enabled": True,
                    "expected_consumers": 2,
                    "healthy_consumers": 2,
                    "dead_worker_slots": [],
                    "active_jobs": 2,
                },
            }

        def start(self):
            return None

        def stop(self, **_kwargs):
            return None

    monkeypatch.setattr(main, "SqsDocumentWorker", BusyWorker)
    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["sqs_worker"]["active_jobs"] == 2


def test_ollama_base_url_environment_setting(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://processor-ollama:11434/")
    monkeypatch.setenv("OLLAMA_URL", "http://legacy-ollama:11434")
    get_settings.cache_clear()
    assert OllamaClient().url == "http://processor-ollama:11434"


def test_legacy_ollama_url_remains_supported(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_URL", "http://host.docker.internal:11434/")
    get_settings.cache_clear()
    assert OllamaClient().url == "http://host.docker.internal:11434"


def test_ollama_model_defaults_to_instruct_when_environment_is_unset(monkeypatch):
    monkeypatch.setattr("app.settings._load_dotenv", lambda: None)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    get_settings.cache_clear()

    assert get_settings()["ollama_model"] == "qwen3-vl:8b-instruct"
    assert OllamaClient().model == "qwen3-vl:8b-instruct"


def test_ollama_model_environment_override_is_preserved(monkeypatch):
    monkeypatch.setattr("app.settings._load_dotenv", lambda: None)
    monkeypatch.setenv("OLLAMA_MODEL", "custom-vision-model:latest")
    get_settings.cache_clear()

    assert get_settings()["ollama_model"] == "custom-vision-model:latest"
    assert OllamaClient().model == "custom-vision-model:latest"


def test_ollama_client_sends_configured_bearer_token(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://la-ea.duckdns.org:11443/")
    monkeypatch.setenv("OLLAMA_API_TOKEN", "unit-test-token")
    get_settings.cache_clear()
    response = type("Response", (), {"status_code": 200, "headers": {"content-type": "application/json"}, "raise_for_status": lambda self: None, "json": lambda self: {"message": {"content": "ok"}}})()
    calls = []
    monkeypatch.setattr("app.ollama_client.requests.post", lambda *args, **kwargs: (calls.append((args, kwargs)) or response))

    assert OllamaClient().chat([{"role": "user", "content": "hello"}]) == "ok"
    assert OllamaClient().url == "https://la-ea.duckdns.org:11443"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer unit-test-token"}


def test_ollama_client_sends_json_schema_as_format(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_TOKEN", raising=False)
    get_settings.cache_clear()
    response = type("Response", (), {"status_code": 200, "headers": {"content-type": "application/json"}, "raise_for_status": lambda self: None, "json": lambda self: {"message": {"content": "{}"}}})()
    calls = []
    monkeypatch.setattr("app.ollama_client.requests.post", lambda *args, **kwargs: (calls.append(kwargs) or response))
    schema = {"type": "object", "required": ["answer"]}

    OllamaClient().chat([{"role": "user", "content": "json"}], format=schema)

    assert calls[0]["json"]["format"] == schema


def test_ollama_client_forwards_retry_generation_options(monkeypatch):
    get_settings.cache_clear()
    response = type("Response", (), {"status_code": 200, "headers": {"content-type": "application/json"}, "raise_for_status": lambda self: None, "json": lambda self: {"message": {"content": "{}"}}})()
    calls = []
    monkeypatch.setattr("app.ollama_client.requests.post", lambda *args, **kwargs: (calls.append(kwargs) or response))

    OllamaClient().chat(
        [{"role": "user", "content": "corrige"}],
        format={"type": "object"},
        options={"temperature": 0},
    )

    assert calls[0]["json"]["options"] == {"temperature": 0}


def test_ollama_client_preserves_safe_response_metadata_without_request_headers(monkeypatch):
    token = "metadata-test-token"
    monkeypatch.setenv("OLLAMA_API_TOKEN", token)
    get_settings.cache_clear()
    response = type("Response", (), {
        "status_code": 200,
        "headers": {"content-type": "application/json"},
        "raise_for_status": lambda self: None,
        "json": lambda self: {
            "message": {"content": "  {\"incomplete\":  "},
            "done": True,
            "done_reason": "length",
            "eval_count": 128,
            "prompt_eval_count": 64,
            "eval_duration": 99,
        },
    })()
    monkeypatch.setattr("app.ollama_client.requests.post", lambda *args, **kwargs: response)

    output = OllamaClient().chat([{"role": "user", "content": "hello"}])

    assert isinstance(output, OllamaOutput)
    assert str(output) == '{"incomplete":'
    assert output.raw_content == '  {"incomplete":  '
    assert output.metadata == {
        "done": True,
        "done_reason": "length",
        "eval_count": 128,
        "prompt_eval_count": 64,
        "eval_duration": 99,
        "http_status": 200,
        "content_type": "application/json",
    }
    assert token not in repr(output.metadata)


def test_ollama_client_accepts_the_real_chat_envelope_shape(monkeypatch):
    """Regression fixture based on the successful local qwen3-vl /api/chat envelope."""
    get_settings.cache_clear()
    envelope = {
        "model": "qwen3-vl:8b",
        "created_at": "2026-10-09T00:00:00Z",
        "message": {"role": "assistant", "content": '{"transcription":"texto"}'},
        "done": True,
        "done_reason": "stop",
        "eval_count": 276,
        "prompt_eval_count": 3,
        "total_duration": 478048208537,
        "load_duration": 128946758,
        "prompt_eval_duration": 76714750,
        "eval_duration": 9489472506,
    }
    monkeypatch.setattr("app.ollama_client.requests.post", lambda *args, **kwargs: FakeHttpResponse(envelope))

    output = OllamaClient().chat([{"role": "user", "content": "same page prompt"}])

    assert str(output) == '{"transcription":"texto"}'
    assert output.metadata == {
        "done": True,
        "done_reason": "stop",
        "eval_count": 276,
        "prompt_eval_count": 3,
        "total_duration": 478048208537,
        "load_duration": 128946758,
        "prompt_eval_duration": 76714750,
        "eval_duration": 9489472506,
        "http_status": 200,
        "content_type": "application/json",
    }


def test_ollama_client_allows_missing_optional_metadata(monkeypatch):
    monkeypatch.setattr(
        "app.ollama_client.requests.post",
        lambda *args, **kwargs: FakeHttpResponse({"message": {"role": "assistant", "content": "{}"}}),
    )

    output = OllamaClient().chat([{"role": "user", "content": "page"}])

    assert output == "{}"
    assert output.metadata == {"http_status": 200, "content_type": "application/json"}


def test_http_error_keeps_status_and_safe_category(monkeypatch, caplog):
    monkeypatch.setattr(
        "app.ollama_client.requests.post",
        lambda *args, **kwargs: FakeHttpResponse({"error": "model not available"}, status_code=503),
    )

    with caplog.at_level(logging.ERROR, logger="app.ollama_client"), pytest.raises(OllamaError) as error:
        OllamaClient().chat([{"role": "user", "content": "page"}])

    assert error.value.category == "http_error"
    assert error.value.status_code == 503
    assert error.value.api_error_present is True
    assert isinstance(error.value.__cause__, RuntimeError)
    assert str(error.value.__cause__) == "HTTPError"
    assert '"error_category":"http_error"' in caplog.text
    assert '"http_status":503' in caplog.text
    assert "model not available" not in caplog.text


def test_missing_content_from_http_200_keeps_the_real_cause(monkeypatch, caplog):
    monkeypatch.setattr(
        "app.ollama_client.requests.post",
        lambda *args, **kwargs: FakeHttpResponse({"message": {"role": "assistant", "content": ""}, "done": True}),
    )

    with caplog.at_level(logging.ERROR, logger="app.ollama_client"), pytest.raises(OllamaError) as error:
        OllamaClient().chat([{"role": "user", "content": "page"}])

    assert error.value.category == "missing_content"
    assert error.value.status_code == 200
    assert "message.content vacío" in str(error.value)
    assert error.value.__cause__ is None
    assert '"error_category":"missing_content"' in caplog.text
    assert "No se pudo procesar la solicitud con Ollama" not in str(error.value)


def test_invalid_envelope_and_content_type_are_classified(monkeypatch):
    responses = iter([
        FakeHttpResponse({"message": []}),
        FakeHttpResponse({"message": {"content": ["not", "text"]}}),
        FakeHttpResponse({"error": "safe generic server error"}),
    ])
    monkeypatch.setattr("app.ollama_client.requests.post", lambda *args, **kwargs: next(responses))
    client = OllamaClient()

    with pytest.raises(OllamaError) as envelope_error:
        client.chat([{"role": "user", "content": "page"}])
    with pytest.raises(OllamaError) as content_error:
        client.chat([{"role": "user", "content": "page"}])
    with pytest.raises(OllamaError) as api_error:
        client.chat([{"role": "user", "content": "page"}])

    assert envelope_error.value.category == "invalid_response_envelope"
    assert content_error.value.category == "invalid_content_type"
    assert api_error.value.category == "ollama_api_error"


@pytest.mark.parametrize(
    ("request_error", "category"),
    [
        (requests.ReadTimeout("timed out"), "read_timeout"),
        (requests.ConnectionError("connection failed"), "connection_error"),
    ],
)
def test_transport_errors_are_classified_and_keep_the_original_cause(monkeypatch, request_error, category):
    monkeypatch.setattr(
        "app.ollama_client.requests.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(request_error),
    )

    with pytest.raises(OllamaError) as error:
        OllamaClient().chat([{"role": "user", "content": "page"}])

    assert error.value.category == category
    assert isinstance(error.value.__cause__, RuntimeError)
    assert str(error.value.__cause__) == type(request_error).__name__


def test_ollama_client_omits_auth_header_without_local_token(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_TOKEN", raising=False)
    get_settings.cache_clear()
    response = type("Response", (), {"status_code": 200, "headers": {"content-type": "application/json"}, "raise_for_status": lambda self: None, "json": lambda self: {"message": {"content": "ok"}}})()
    calls = []
    monkeypatch.setattr("app.ollama_client.requests.post", lambda *args, **kwargs: (calls.append(kwargs) or response))

    assert OllamaClient().chat([{"role": "user", "content": "hello"}]) == "ok"
    assert calls[0]["headers"] is None


def test_ollama_token_is_not_logged_or_included_in_exception(monkeypatch, caplog):
    token = "sensitive-test-token"
    monkeypatch.setenv("OLLAMA_API_TOKEN", token)
    get_settings.cache_clear()

    def fail_request(*args, **kwargs):
        raise requests.ConnectionError(f"connection failed with {token}")

    monkeypatch.setattr("app.ollama_client.requests.post", fail_request)
    with caplog.at_level(logging.ERROR), pytest.raises(OllamaError) as error:
        OllamaClient().chat([{"role": "user", "content": "hello"}])

    assert token not in caplog.text
    assert token not in str(error.value)
    assert token not in repr(error.value.__cause__)
    assert token not in repr(error.value.__context__)


def test_one_page_and_empty_prompt(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr("app.document_service.OllamaClient", lambda: fake)
    result = process_pdf("one.pdf", make_pdf(), "", None)
    assert result["pages"] == 1
    assert result["page_extractions"] == [{
        "page": 1,
        "transcription": "Encabezado 1\nLínea literal 1",
        "analysis_notes": "Análisis 1",
        "visual_elements": {"stamps": [], "signatures": [], "other_marks": []},
    }]
    assert result["analysis"]["document_type"] == "Documento de prueba"
    assert result["transcription"]["text"] == "--- Página 1 ---\nEncabezado 1\nLínea literal 1"
    assert result["visual_elements"] == {"stamps": [], "signatures": [], "other_marks": []}
    assert result["requested_output"] == {"user_request": "", "response": None}
    assert fake.calls[-1][1] is None
    assert "transcription" not in fake.formats[-1]["properties"]
    assert "requested_output" in fake.formats[-1]["required"]
    assert fake.formats[-1]["properties"]["requested_output"]["$ref"].endswith("GeneratedRequestedOutput")


def test_multipage_sequential_context_and_prompt(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr("app.document_service.OllamaClient", lambda: fake)
    result = process_pdf("many.pdf", make_pdf(3), "Identifica cantidades", None)
    assert result["pages"] == 3
    assert [item["page"] for item in result["page_extractions"]] == [1, 2, 3]
    assert result["page_extractions"][2]["transcription"] == "Encabezado 3\nLínea literal 3"
    assert result["transcription"]["text"] == (
        "--- Página 1 ---\nEncabezado 1\nLínea literal 1\n\n"
        "--- Página 2 ---\nEncabezado 2\nLínea literal 2\n\n"
        "--- Página 3 ---\nEncabezado 3\nLínea literal 3"
    )
    assert result["requested_output"]["user_request"] == "Identifica cantidades"
    assert result["requested_output"]["response"] == "Respuesta simulada"
    assert "Identifica cantidades" in fake.calls[-1][0][0]["content"]
    assert "página previa 1" in fake.calls[1][0][0]["content"]
    assert "página previa 2" in fake.calls[2][0][0]["content"]
    assert fake.calls[-1][1] is None
    assert fake.formats[0] == fake.formats[1] == fake.formats[2]


def valid_page_output(**overrides):
    output = {
        "transcription": "Línea uno\nLínea dos",
        "analysis_notes": "Fecha visible",
        "visual_elements": {"stamps": [], "signatures": [], "other_marks": []},
        "context_summary": "Oficio administrativo",
    }
    output.update(overrides)
    return json.dumps(output, ensure_ascii=False)


def valid_final_output(**overrides):
    output = {
        "analysis": {"document_type": "Oficio", "summary": "Resumen", "key_points": []},
        "visual_elements": {"stamps": [], "signatures": [], "other_marks": []},
        "requested_output": {"response": "Respuesta basada en el documento"},
    }
    output.update(overrides)
    return json.dumps(output, ensure_ascii=False)


def test_page_extraction_validates_on_first_attempt():
    from app.schemas import PageExtraction

    client = FakeOllama(responses=[valid_page_output()])
    result = _generate_validated(client, [{"role": "user", "content": "extraer"}], PageExtraction, image=b"image")

    assert result.transcription == "Línea uno\nLínea dos"
    assert len(client.calls) == 1
    assert client.formats[0] == PageExtraction.model_json_schema()
    assert client.options == [{"num_ctx": 16384, "num_predict": 6000}]


def test_page_and_synthesis_use_independent_generation_budgets(monkeypatch):
    from app.schemas import DocumentAnalysisGeneration, PageExtraction

    monkeypatch.setenv("OLLAMA_PAGE_NUM_PREDICT", "4500")
    monkeypatch.setenv("OLLAMA_SYNTHESIS_NUM_PREDICT", "1250")
    get_settings.cache_clear()
    page_client = FakeOllama(responses=[valid_page_output()])
    synthesis_client = FakeOllama(responses=[valid_final_output()])

    _generate_validated(page_client, [{"role": "user", "content": "page"}], PageExtraction)
    _generate_validated(
        synthesis_client,
        [{"role": "user", "content": "synthesis"}],
        DocumentAnalysisGeneration,
    )

    assert page_client.options == [{"num_ctx": 16384, "num_predict": 4500}]
    assert synthesis_client.options == [{"num_ctx": 16384, "num_predict": 1250}]


@pytest.mark.parametrize(
    ("variable", "invalid_value"),
    [
        ("OLLAMA_PAGE_NUM_PREDICT", "0"),
        ("OLLAMA_PAGE_NUM_PREDICT", "-1"),
        ("OLLAMA_PAGE_NUM_PREDICT", "1.5"),
        ("OLLAMA_PAGE_NUM_PREDICT", "not-a-number"),
        ("OLLAMA_SYNTHESIS_NUM_PREDICT", "0"),
    ],
)
def test_num_predict_settings_must_be_positive_integers(monkeypatch, variable, invalid_value):
    monkeypatch.setenv(variable, invalid_value)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match=f"{variable} must be a positive integer"):
        get_settings()


def test_num_predict_defaults_and_read_timeout(monkeypatch):
    monkeypatch.delenv("OLLAMA_PAGE_NUM_PREDICT", raising=False)
    monkeypatch.delenv("OLLAMA_SYNTHESIS_NUM_PREDICT", raising=False)
    monkeypatch.delenv("OLLAMA_READ_TIMEOUT", raising=False)
    get_settings.cache_clear()

    configured = get_settings()
    assert configured["ollama_num_ctx"] == 16384
    assert configured["ollama_page_num_predict"] == 6000
    assert configured["ollama_synthesis_num_predict"] == 2000
    assert configured["ollama_read_timeout"] == 1800


def test_read_timeout_override_reaches_ollama_client(monkeypatch):
    monkeypatch.setenv("OLLAMA_READ_TIMEOUT", "2100")
    get_settings.cache_clear()
    assert OllamaClient().timeout == (10, 2100)


def test_final_synthesis_accepts_normal_completion_with_its_own_budget():
    raw = OllamaOutput(
        valid_final_output(),
        {"done": True, "done_reason": "stop", "eval_count": 1999},
    )
    client = FakeOllama(responses=[valid_page_output(), raw])

    result = process_pdf("synthetic.pdf", make_pdf(), "", client)

    assert result["pages"] == 1
    assert client.options == [
        {"num_ctx": 16384, "num_predict": 6000},
        {"num_ctx": 16384, "num_predict": 2000},
    ]


def test_checkpoint_and_result_fingerprints_include_relevant_generation_budgets():
    from app.document_service import _checkpoint_fingerprint, result_pipeline_fingerprint

    base = get_settings().copy()
    base_checkpoint = _checkpoint_fingerprint(base)
    base_result = result_pipeline_fingerprint("prompt", base)

    page_changed = {**base, "ollama_page_num_predict": base["ollama_page_num_predict"] + 1}
    synthesis_changed = {**base, "ollama_synthesis_num_predict": base["ollama_synthesis_num_predict"] + 1}

    assert _checkpoint_fingerprint(page_changed) != base_checkpoint
    assert _checkpoint_fingerprint(synthesis_changed) == base_checkpoint
    assert result_pipeline_fingerprint("prompt", synthesis_changed) != base_result


@pytest.mark.parametrize(
    ("metadata", "expected_status", "expected_error"),
    [
        ({"done": True, "done_reason": "stop", "eval_count": 5999}, "normal", None),
        ({"done": True, "done_reason": "length", "eval_count": 6000}, "limit_reached", "output_limit_reached"),
        ({"done": False, "eval_count": 200}, "confirmed_incomplete", "output_incomplete"),
        ({"done": True, "eval_count": 6000}, "possible_limit_reached", "possible_output_limit"),
        ({}, "metadata_unavailable", None),
    ],
)
def test_generation_termination_signals_are_classified(metadata, expected_status, expected_error):
    from app.document_service import _generation_termination

    assert _generation_termination(metadata, 6000) == (expected_status, expected_error)


def test_invalid_json_retries_with_schema_and_same_sampling_then_accepts_valid_output(caplog):
    from app.schemas import PageExtraction

    client = FakeOllama(responses=["no es JSON confidencial", valid_page_output()])
    with caplog.at_level(logging.WARNING, logger="app.document_service"):
        result = _generate_validated(client, [{"role": "user", "content": "extraer"}], PageExtraction, image=b"image")

    assert result.transcription == "Línea uno\nLínea dos"
    assert len(client.calls) == 2
    assert client.formats[0] == client.formats[1] == PageExtraction.model_json_schema()
    assert client.options == [
        {"num_ctx": 16384, "num_predict": 6000},
        {"num_ctx": 16384, "num_predict": 6000},
    ]
    assert "no es JSON confidencial" not in client.calls[1][0][-1]["content"]
    assert "JSON Schema" in client.calls[1][0][-1]["content"]
    assert client.calls[1][1] == b"image"
    assert '"validation_error_category":"invalid_json"' in caplog.text
    assert '"error_type":"JSONDecodeError"' in caplog.text
    assert '"parser_position":' in caplog.text
    assert "no es JSON confidencial" not in caplog.text


def test_debug_model_output_is_disabled_by_default_and_never_logs_raw_content(monkeypatch, caplog):
    from app.schemas import PageExtraction

    monkeypatch.delenv("DEBUG_MODEL_OUTPUT", raising=False)
    get_settings.cache_clear()
    sensitive_raw = OllamaOutput(
        '{"transcription":"SECRET_DOCUMENT_TEXT"',
        {"done": True, "done_reason": "stop", "eval_count": 17},
    )
    client = FakeOllama(responses=[sensitive_raw, valid_page_output()])

    with caplog.at_level(logging.WARNING, logger="app.document_service"):
        _generate_validated(client, [{"role": "user", "content": "private prompt"}], PageExtraction)

    assert "OLLAMA_DIAGNOSTIC_RAW_RESPONSE" not in caplog.text
    assert "SECRET_DOCUMENT_TEXT" not in caplog.text
    assert "done_reason='length'" not in caplog.text
    assert "private prompt" not in caplog.text


def test_debug_model_output_logs_exact_failed_response_only_when_enabled(monkeypatch, caplog):
    from app.schemas import PageExtraction

    token = "debug-must-not-leak-token"
    monkeypatch.setenv("DEBUG_MODEL_OUTPUT", "true")
    monkeypatch.setenv("OLLAMA_API_TOKEN", token)
    get_settings.cache_clear()
    raw = "A" * 600 + "SECRET_MIDDLE" + "Z" * 600
    output = OllamaOutput(
        raw,
        {
            "done": True,
            "done_reason": "stop",
            "eval_count": 321,
            "prompt_eval_count": 123,
            "total_duration": 987,
            "load_duration": 12,
            "prompt_eval_duration": 34,
            "eval_duration": 56,
        },
    )
    client = FakeOllama(responses=[output, valid_page_output()])

    with caplog.at_level(logging.WARNING, logger="app.document_service"):
        _generate_validated(client, [{"role": "user", "content": "private prompt"}], PageExtraction)

    assert "OLLAMA_DIAGNOSTIC_RAW_RESPONSE" in caplog.text
    assert '"response_content":' in caplog.text
    assert raw in caplog.text
    assert '"raw_length":1213' in caplog.text
    assert '"done":true' in caplog.text
    assert '"done_reason":"stop"' in caplog.text
    assert '"eval_count":321' in caplog.text
    assert '"prompt_eval_count":123' in caplog.text
    assert '"total_duration":987' in caplog.text
    assert token not in caplog.text
    assert "private prompt" not in caplog.text


def test_page_events_correlate_exact_page_attempt_and_request_without_document_text(caplog):
    from app.diagnostics import document_context

    fake = FakeOllama(fail_on_call=2)
    with document_context(
        "synthetic-trace-job",
        sqs_message_id="synthetic-sqs-message",
        sqs_receive_count=2,
        processing_attempt=2,
        ecs_task_id="synthetic-task",
    ):
        with caplog.at_level(logging.INFO, logger="app.document_service"):
            with pytest.raises(OllamaError):
                process_pdf("synthetic.pdf", make_pdf(2, "PRIVATE_SYNTHETIC_TEXT"), "PRIVATE_PROMPT", fake)

    assert '"event":"OLLAMA_REQUEST_START"' in caplog.text
    assert '"event":"OLLAMA_REQUEST_FAILED"' in caplog.text
    assert '"event":"PAGE_PROCESSING_FAILED"' in caplog.text
    assert '"job_id":"synthetic-trace-job"' in caplog.text
    assert '"sqs_receive_count":2' in caplog.text
    assert '"ecs_task_id":"synthetic-task"' in caplog.text
    assert '"page_number":2' in caplog.text
    assert '"ollama_request_id":"' in caplog.text
    assert "PRIVATE_SYNTHETIC_TEXT" not in caplog.text
    assert "PRIVATE_PROMPT" not in caplog.text


def test_page_validation_error_retries_and_logs_only_safe_paths(caplog):
    from app.schemas import PageExtraction

    invalid = json.loads(valid_page_output())
    invalid["visual_elements"]["stamps"] = [{
        "description": "sello",
        "visible_text": {"confidential_document_value": "SECRET_DOCUMENT_TEXT"},
    }]
    client = FakeOllama(responses=[json.dumps(invalid), valid_page_output()])

    with caplog.at_level(logging.WARNING, logger="app.document_service"):
        _generate_validated(client, [{"role": "user", "content": "prompt privado"}], PageExtraction, image=b"private image")

    assert '"event":"OLLAMA_RESPONSE_SCHEMA_INVALID"' in caplog.text
    assert '"error_count":1' in caplog.text
    assert '"field_path":"visual_elements.stamps.0.visible_text"' in caplog.text
    assert '"error_type":"string_type"' in caplog.text
    assert "SECRET_DOCUMENT_TEXT" not in caplog.text
    assert "confidential_document_value" not in caplog.text
    assert "prompt privado" not in caplog.text
    assert "private image" not in caplog.text


def test_invalid_json_and_schema_failures_are_distinguished_without_raw_context():
    from app.schemas import PageExtraction

    with pytest.raises(StructuredOutputError) as json_error:
        _validate_model_output('{"text": "PRIVATE_DOCUMENT_TEXT"', PageExtraction)
    assert json_error.value.kind == "invalid_json"
    assert json_error.value.position is not None
    assert json_error.value.parser_message
    assert "PRIVATE_DOCUMENT_TEXT" not in str(json_error.value)
    assert json_error.value.__context__ is None

    with pytest.raises(StructuredOutputError) as schema_error:
        _validate_model_output('{"transcription": 55}', PageExtraction)
    assert schema_error.value.kind == "schema_invalid"
    assert schema_error.value.errors[0] == {
        "path": "transcription",
        "type": "string_type",
        "message": "Input should be a valid string",
    }


def test_invalid_output_after_retry_raises_after_exactly_two_calls():
    from app.schemas import PageExtraction

    client = FakeOllama(responses=["not JSON", "still not JSON"])
    with pytest.raises(OllamaError, match="después de 2 intentos"):
        _generate_validated(client, [{"role": "user", "content": "extraer"}], PageExtraction)
    assert MAX_STRUCTURED_OUTPUT_ATTEMPTS == 2
    assert len(client.calls) == 2


def test_visual_optional_values_and_empty_arrays_have_contract_defaults():
    from app.schemas import PageExtraction

    raw = valid_page_output(visual_elements={"stamps": [{"description": "Sello visible"}]})
    parsed = _validate_model_output(raw, PageExtraction)
    assert parsed.visual_elements.stamps[0].visible_text is None
    assert parsed.visual_elements.stamps[0].location is None
    assert parsed.visual_elements.signatures == []
    assert parsed.visual_elements.other_marks == []

    explicit_null = valid_page_output(visual_elements={
        "stamps": [{"description": "Sello", "visible_text": None, "location": None}],
        "signatures": [],
        "other_marks": [],
    })
    assert _validate_model_output(explicit_null, PageExtraction).visual_elements.stamps[0].visible_text is None


def test_null_requested_response_is_valid_in_generation_schema():
    from app.schemas import DocumentAnalysisGeneration

    raw = valid_final_output(requested_output={"response": None})
    parsed = _validate_model_output(raw, DocumentAnalysisGeneration)
    assert parsed.requested_output.response is None


def test_schema_still_rejects_unrecognized_extra_fields():
    from app.schemas import PageExtraction

    invalid = json.loads(valid_page_output())
    invalid["unexpected_document_property"] = "PRIVATE"
    with pytest.raises(StructuredOutputError) as error:
        _validate_model_output(json.dumps(invalid), PageExtraction)
    assert error.value.kind == "schema_invalid"
    assert error.value.errors[0]["type"] == "extra_forbidden"
    assert error.value.errors[0]["path"] == "<extra>"
    assert "unexpected_document_property" not in str(error.value)


def test_final_synthesis_uses_bounded_retry_and_backend_owns_exact_user_request(monkeypatch):
    user_request = "  Resume exactamente este documento, sin alterar esta solicitud. \n"
    invalid_final = json.loads(valid_final_output())
    invalid_final["analysis"]["key_points"] = "esto no es una lista"
    fake = FakeOllama(responses=[
        valid_page_output(),
        json.dumps(invalid_final),
        valid_final_output(),
    ])
    result = process_pdf("retry.pdf", make_pdf(), user_request, fake)

    assert len(fake.calls) == 3
    assert fake.options == [
        {"num_ctx": 16384, "num_predict": 6000},
        {"num_ctx": 16384, "num_predict": 2000},
        {"num_ctx": 16384, "num_predict": 2000},
    ]
    assert fake.formats[1] == fake.formats[2]
    assert "Rutas que deben revisarse: analysis.key_points (list_type)." in fake.calls[2][0][-1]["content"]
    assert result["requested_output"]["user_request"] == user_request
    assert result["requested_output"]["response"] == "Respuesta basada en el documento"
    assert "user_request" not in fake.formats[1]["$defs"]["GeneratedRequestedOutput"]["properties"]


def test_page_output_failure_returns_502_after_two_attempts(monkeypatch):
    fake = FakeOllama(responses=["invalid", "still invalid"])
    monkeypatch.setattr(main, "process_pdf", lambda filename, contents, user_prompt, client: process_pdf(
        filename, contents, user_prompt, fake
    ))

    response = TestClient(main.app).post(
        "/process",
        files={"file": ("document.pdf", make_pdf(), "application/pdf")},
    )
    assert response.status_code == 502
    assert len(fake.calls) == 2


def test_endpoint_accepts_user_prompt(monkeypatch):
    expected = {
        "filename": "document.pdf",
        "pages": 1,
        "analysis": {"document_type": "Carta", "summary": "ok", "key_points": []},
        "transcription": {"text": "texto"},
        "visual_elements": {"stamps": [], "signatures": [], "other_marks": []},
        "requested_output": {"user_request": "Resume", "response": "ok"},
    }
    monkeypatch.setattr(main, "process_pdf", lambda filename, contents, user_prompt, client: {**expected, "user_prompt": user_prompt})
    response = TestClient(main.app).post("/process", files={"file": ("document.pdf", make_pdf(), "application/pdf")}, data={"user_prompt": "Resume"})
    assert response.status_code == 200
    assert response.json()["user_prompt"] == "Resume"


def test_empty_and_oversized_uploads():
    client = TestClient(main.app)
    empty = client.post("/process", files={"file": ("empty.pdf", b"", "application/pdf")})
    too_large = client.post("/process", files={"file": ("large.pdf", b"x" * 2_000_001, "application/pdf")})
    assert empty.status_code == 400
    assert too_large.status_code == 413


def test_zero_page_document_is_rejected(monkeypatch):
    class EmptyDocument:
        page_count = 0

        def close(self):
            pass

    monkeypatch.setattr("app.document_service.fitz.open", lambda **kwargs: EmptyDocument())
    with pytest.raises(ValueError, match="no contiene páginas"):
        process_pdf("no-pages.pdf", b"valid empty PDF placeholder", "", FakeOllama())


def test_ollama_failure_is_provider_error(monkeypatch):
    monkeypatch.setattr(main, "process_pdf", lambda *args: (_ for _ in ()).throw(OllamaError("provider unavailable")))
    response = TestClient(main.app).post("/process", files={"file": ("document.pdf", make_pdf(), "application/pdf")})
    assert response.status_code == 502
    assert "stack" not in response.text.lower()


def test_provider_token_is_not_logged_or_returned_by_endpoint(monkeypatch, caplog):
    token = "sensitive-endpoint-test-token"
    monkeypatch.setattr(main, "process_pdf", lambda *args: (_ for _ in ()).throw(OllamaError(token)))

    with caplog.at_level(logging.ERROR):
        response = TestClient(main.app).post(
            "/process",
            files={"file": ("document.pdf", make_pdf(), "application/pdf")},
        )

    assert response.status_code == 502
    assert token not in caplog.text
    assert token not in response.text


def test_invalid_pdf_is_rejected():
    response = TestClient(main.app).post("/process", files={"file": ("invalid.pdf", b"not a pdf", "application/pdf")})
    assert response.status_code == 400


def test_process_pdf_reports_progress_after_each_completed_page(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr("app.document_service.OllamaClient", lambda: fake)
    progress = []

    process_pdf("progress.pdf", make_pdf(2), "", None, lambda current, total: progress.append((current, total)))

    assert progress == [(0, 2), (1, 2), (2, 2)]


def test_jobs_and_pages_have_distinct_images_and_local_synthesis():
    """Exercise the actual PDF renderer and requests payload, not a permissive chat mock."""
    import base64
    from unittest.mock import patch
    from app.diagnostics import document_context

    payloads = []
    def respond(_url, **kwargs):
        payload = kwargs['json']
        payloads.append(payload)
        if 'images' in payload['messages'][-1]:
            extraction = json.loads(valid_page_output())
            extraction['transcription'] = f'Synthetic extraction {len(payloads)}'
            return FakeHttpResponse({'message': {'content': json.dumps(extraction)}, 'done': True})
        return FakeHttpResponse({'message': {'content': valid_final_output()}, 'done': True})

    with patch('app.ollama_client.requests.post', side_effect=respond):
        with document_context('synthetic-job-a'):
            process_pdf('a.pdf', make_pdf(2), 'request-a', OllamaClient())
        with document_context('synthetic-job-b'):
            process_pdf('b.pdf', make_pdf(1, label='Other synthetic document'), 'request-b', OllamaClient())

    for payload in (payloads[0], payloads[1], payloads[3]):
        assert len(payload['messages']) == 1
        assert len(payload['messages'][0]['images']) == 1
        image = base64.b64decode(payload['messages'][0]['images'][0])
        assert image.startswith(b'\x89PNG')
    assert payloads[0]['messages'][0]['images'] != payloads[1]['messages'][0]['images']
    assert payloads[0]['messages'][0]['images'] != payloads[3]['messages'][0]['images']
    # Independent payload objects as well as distinct synthetic document pixels.
    assert payloads[0]['messages'] is not payloads[3]['messages']
    assert payloads[0]['messages'][0]['images'] is not payloads[3]['messages'][0]['images']
    assert 'request-a' in payloads[2]['messages'][0]['content']
    assert 'request-a' not in payloads[4]['messages'][0]['content']
    assert 'request-b' in payloads[4]['messages'][0]['content']
    assert '"page": 2' in payloads[2]['messages'][0]['content']
    assert '"page": 2' not in payloads[4]['messages'][0]['content']
    assert 'Synthetic extraction 1' in payloads[2]['messages'][0]['content']
    assert 'Synthetic extraction 2' in payloads[2]['messages'][0]['content']
    assert 'Synthetic extraction 1' not in payloads[4]['messages'][0]['content']
    assert 'Synthetic extraction 2' not in payloads[4]['messages'][0]['content']
    assert 'Synthetic extraction 4' in payloads[4]['messages'][0]['content']


def test_two_concurrent_document_runs_keep_page_context_and_synthesis_isolated():
    """Two mocked jobs can overlap without sharing extraction state or history."""
    import re
    import threading
    from app.diagnostics import document_context

    first_page_barrier = threading.Barrier(2)

    class JobScopedOllama:
        def __init__(self, label):
            self.label = label
            self.page_prompts = []
            self.page_images = []
            self.synthesis_prompt = None

        def chat(self, messages, image=None, **_kwargs):
            prompt = messages[-1]["content"]
            if image is not None:
                self.page_prompts.append(prompt)
                self.page_images.append(image)
                page = int(re.search(r"Página (\d+) de", prompt).group(1))
                if page == 1:
                    first_page_barrier.wait(timeout=2)
                return valid_page_output(
                    transcription=f"{self.label} transcription page {page}",
                    analysis_notes=f"{self.label} notes page {page}",
                    context_summary=f"{self.label} context page {page}",
                )
            self.synthesis_prompt = prompt
            return valid_final_output(
                analysis={"document_type": "Synthetic", "summary": f"summary-{self.label}", "key_points": []}
            )

    clients = {"job-a": JobScopedOllama("A_ONLY"), "job-b": JobScopedOllama("B_ONLY")}
    outputs = {}

    def run(job_id, filename, page_count, label):
        with document_context(job_id):
            outputs[job_id] = process_pdf(
                filename,
                make_pdf(page_count, f"Synthetic {label}"),
                f"request-{label}",
                clients[job_id],
                job_id=job_id,
            )

    threads = [
        threading.Thread(target=run, args=("job-a", "a.pdf", 2, "A_ONLY")),
        threading.Thread(target=run, args=("job-b", "b.pdf", 1, "B_ONLY")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4)

    assert all(not thread.is_alive() for thread in threads)
    assert outputs["job-a"]["analysis"]["summary"] == "summary-A_ONLY"
    assert outputs["job-b"]["analysis"]["summary"] == "summary-B_ONLY"
    assert len(clients["job-a"].page_prompts) == 2
    assert len(clients["job-b"].page_prompts) == 1
    assert "Página 1 de 2" in clients["job-a"].page_prompts[0]
    assert "Página 2 de 2" in clients["job-a"].page_prompts[1]
    assert "A_ONLY context page 1" in clients["job-a"].page_prompts[1]
    assert "B_ONLY" not in " ".join(clients["job-a"].page_prompts)
    assert "A_ONLY" not in " ".join(clients["job-b"].page_prompts)
    assert len(set(clients["job-a"].page_images + clients["job-b"].page_images)) == 3
    assert "A_ONLY transcription page 1" in clients["job-a"].synthesis_prompt
    assert "A_ONLY transcription page 2" in clients["job-a"].synthesis_prompt
    assert "B_ONLY" not in clients["job-a"].synthesis_prompt
    assert "B_ONLY transcription page 1" in clients["job-b"].synthesis_prompt
    assert "A_ONLY" not in clients["job-b"].synthesis_prompt


def test_retry_keeps_full_instructions_and_original_image_in_one_message(monkeypatch):
    import base64
    from app.schemas import PageExtraction
    captured = []
    def respond(_url, **kwargs):
        captured.append(kwargs['json'])
        content = 'invalid private output' if len(captured) == 1 else valid_page_output()
        return FakeHttpResponse({'message': {'content': content}})
    monkeypatch.setattr('app.ollama_client.requests.post', respond)
    messages = [{'role': 'user', 'content': 'Read only this image. private original prompt'}]
    _generate_validated(OllamaClient(), messages, PageExtraction, image=b'original image bytes')
    assert messages == [{'role': 'user', 'content': 'Read only this image. private original prompt'}]
    assert len(captured[1]['messages']) == 1
    assert captured[0]['messages'][0]['images'] == captured[1]['messages'][0]['images']
    assert base64.b64decode(captured[1]['messages'][0]['images'][0]) == b'original image bytes'
    assert 'Read only this image.' in captured[1]['messages'][0]['content']
    assert 'invalid private output' not in captured[1]['messages'][0]['content']


def test_reasoning_is_never_used_as_content(monkeypatch):
    monkeypatch.setattr('app.ollama_client.requests.post', lambda *_args, **_kwargs: FakeHttpResponse({
        'message': {'content': valid_page_output(), 'thinking': 'PRIVATE REASONING'},
        'done': True, 'eval_count': 17,
    }))
    raw = OllamaClient().chat([{'role': 'user', 'content': 'Read this image'}], image=b'image')
    from app.schemas import PageExtraction
    assert _validate_model_output(raw, PageExtraction).transcription == 'Línea uno\nLínea dos'
    assert 'PRIVATE REASONING' not in raw
    assert raw.metadata['thinking_chars'] == len('PRIVATE REASONING')


def test_structured_calls_configure_context_and_page_token_cap_without_forcing_reasoning(monkeypatch):
    from app.schemas import PageExtraction
    captured = []
    def respond(_url, **kwargs):
        captured.append(kwargs['json'])
        return FakeHttpResponse({'message': {'content': valid_page_output()}})
    monkeypatch.setenv('OLLAMA_NUM_CTX', '12288')
    get_settings.cache_clear()
    monkeypatch.setattr('app.ollama_client.requests.post', respond)
    _generate_validated(OllamaClient(), [{'role': 'user', 'content': 'Synthetic extraction'}],
                        PageExtraction, image=b'synthetic image')
    assert captured[0]['options'] == {'num_ctx': 12288, 'num_predict': 6000}
    assert captured[0]['truncate'] is False
    assert captured[0]['shift'] is False
    assert 'think' not in captured[0]


def test_client_does_not_share_nested_image_lists_with_caller(monkeypatch):
    captured = []
    def respond(_url, **kwargs):
        captured.append(kwargs['json'])
        return FakeHttpResponse({'message': {'content': valid_page_output()}})
    monkeypatch.setattr('app.ollama_client.requests.post', respond)
    messages = [{'role': 'user', 'content': 'Synthetic', 'images': ['first-image']}]
    client = OllamaClient()
    client.chat(messages)
    messages[0]['images'][0] = 'second-image'
    client.chat(messages)
    assert captured[0]['messages'][0]['images'] == ['first-image']
    assert captured[1]['messages'][0]['images'] == ['second-image']
    assert captured[0]['messages'] is not captured[1]['messages']
    assert 'truncate' not in captured[0]
    assert 'shift' not in captured[0]


def test_safe_image_and_synthesis_diagnostics_do_not_log_content(caplog):
    from app.diagnostics import document_context
    with document_context('synthetic-job'):
        with caplog.at_level(logging.INFO, logger='app.document_service'):
            process_pdf('synthetic.pdf', make_pdf(), 'PRIVATE REQUEST', FakeOllama())
    assert '"job_id":"synthetic-job"' in caplog.text
    assert '"image_sha256":"' in caplog.text
    assert '"image_width":' in caplog.text
    assert '"extraction_count":1' in caplog.text
    assert 'PRIVATE REQUEST' not in caplog.text
    assert 'Encabezado 1' not in caplog.text
    from app.diagnostics import current_job_id
    assert current_job_id() == 'local'


def test_valid_page_checkpoints_are_saved_after_validation_and_progress(monkeypatch):
    store = MemoryPageCheckpointStore()
    progress = []
    fake = FakeOllama()
    monkeypatch.setattr("app.document_service.OllamaClient", lambda: fake)

    process_pdf(
        "synthetic.pdf", make_pdf(2), "", fake,
        lambda current, total: progress.append((current, total, len(store.objects))),
        job_id=store.job_id,
        checkpoint_store=store,
    )

    assert len(store.objects) == 2
    first = next(value for key, value in store.objects.items() if "page-0001/" in key)
    assert first["job_id"] == store.job_id
    assert first["page_number"] == 1
    assert first["total_pages"] == 2
    assert first["schema_version"] == "page-extraction-v1"
    assert len(first["pdf_sha256"]) == 64
    assert len(first["pipeline_fingerprint"]) == 64
    assert first["extraction"]["transcription"] == "Encabezado 1\nLínea literal 1"
    assert progress == [(0, 2, 0), (1, 2, 1), (2, 2, 2)]


def test_invalid_model_output_is_never_checkpointed():
    store = MemoryPageCheckpointStore()
    fake = FakeOllama(responses=["invalid", "still invalid"])

    with pytest.raises(OllamaError):
        process_pdf(
            "synthetic.pdf", make_pdf(), "", fake,
            job_id=store.job_id,
            checkpoint_store=store,
        )

    assert store.objects == {}


def test_reached_generation_limit_is_rejected_without_structured_retry(caplog):
    from app.schemas import PageExtraction

    raw = OllamaOutput(
        valid_page_output(),
        {"done": True, "done_reason": "length", "eval_count": 6000, "total_duration": 123},
    )
    client = FakeOllama(responses=[raw, valid_page_output()])
    with caplog.at_level(logging.ERROR, logger="app.document_service"):
        with pytest.raises(OllamaError) as error:
            _generate_validated(client, [{"role": "user", "content": "page"}], PageExtraction, page=4)

    assert error.value.category == "output_limit_reached"
    assert len(client.calls) == 1
    assert '"event":"OLLAMA_OUTPUT_LIMIT_REACHED"' in caplog.text
    assert '"truncation_status":"limit_reached"' in caplog.text
    assert '"num_predict":6000' in caplog.text
    assert valid_page_output() not in caplog.text


def test_normal_termination_near_token_budget_is_accepted():
    from app.schemas import PageExtraction

    raw = OllamaOutput(
        valid_page_output(),
        {"done": True, "done_reason": "stop", "eval_count": 5999},
    )
    client = FakeOllama(responses=[raw])
    result = _generate_validated(client, [{"role": "user", "content": "page"}], PageExtraction)

    assert result.transcription == "Línea uno\nLínea dos"
    assert len(client.calls) == 1


def test_ambiguous_exact_budget_without_stop_signal_is_rejected():
    from app.schemas import PageExtraction

    raw = OllamaOutput(valid_page_output(), {"done": True, "eval_count": 6000})
    client = FakeOllama(responses=[raw])
    with pytest.raises(OllamaError) as error:
        _generate_validated(client, [{"role": "user", "content": "page"}], PageExtraction)

    assert error.value.category == "possible_output_limit"
    assert len(client.calls) == 1


def test_done_false_response_is_rejected_before_json_or_pydantic_validation(caplog):
    from app.schemas import PageExtraction

    raw = OllamaOutput(valid_page_output(), {"done": False})
    client = FakeOllama(responses=[raw, valid_page_output()])
    with caplog.at_level(logging.ERROR, logger="app.document_service"):
        with pytest.raises(OllamaError) as error:
            _generate_validated(client, [{"role": "user", "content": "page"}], PageExtraction)

    assert error.value.category == "output_incomplete"
    assert len(client.calls) == 1
    assert '"event":"OLLAMA_OUTPUT_INCOMPLETE"' in caplog.text
    assert '"truncation_status":"confirmed_incomplete"' in caplog.text


def test_failure_on_page_four_resumes_from_page_four_without_repeating_prior_pages(monkeypatch):
    store = MemoryPageCheckpointStore()
    pdf = make_pdf(5)
    first_attempt = FakeOllama(fail_on_call=4)
    monkeypatch.setattr("app.document_service.OllamaClient", lambda: first_attempt)

    with pytest.raises(OllamaError):
        process_pdf(
            "synthetic.pdf", pdf, "", first_attempt,
            job_id=store.job_id,
            checkpoint_store=store,
        )

    assert len(store.objects) == 3
    assert sum(image is not None for _messages, image in first_attempt.calls) == 4

    second_attempt = FakeOllama()
    progress = []
    result = process_pdf(
        "synthetic.pdf", pdf, "", second_attempt,
        lambda current, total: progress.append((current, total)),
        job_id=store.job_id,
        checkpoint_store=store,
    )

    page_calls = [(messages, image) for messages, image in second_attempt.calls if image is not None]
    assert len(page_calls) == 2
    assert "Página 4 de 5" in page_calls[0][0][0]["content"]
    assert "Página 5 de 5" in page_calls[1][0][0]["content"]
    assert progress[0] == (3, 5)
    assert result["pages"] == 5
    assert "Encabezado 1" in result["transcription"]["text"]
    synthesis_content = second_attempt.calls[-1][0][0]["content"]
    page_positions = [synthesis_content.index(f'"page": {page}') for page in range(1, 6)]
    assert page_positions == sorted(page_positions)


def test_truncated_page_does_not_checkpoint_or_advance_and_keeps_prior_checkpoints():
    store = MemoryPageCheckpointStore()
    pdf = make_pdf(3)
    progress = []
    truncated = OllamaOutput(
        valid_page_output(),
        {"done": True, "done_reason": "length", "eval_count": 6000},
    )
    first_delivery = FakeOllama(responses=[valid_page_output(), valid_page_output(), truncated])

    with pytest.raises(OllamaError) as error:
        process_pdf(
            "synthetic.pdf",
            pdf,
            "",
            first_delivery,
            lambda current, total: progress.append((current, total)),
            job_id=store.job_id,
            checkpoint_store=store,
        )

    assert error.value.category == "output_limit_reached"
    assert len(store.manifest["pages"]) == 2
    assert progress == [(0, 3), (1, 3), (2, 3)]
    prior_keys = {entry["key"] for entry in store.manifest["pages"].values()}
    assert len(prior_keys) == 2

    retry = FakeOllama()
    process_pdf("synthetic.pdf", pdf, "", retry, job_id=store.job_id, checkpoint_store=store)
    assert [image is not None for _messages, image in retry.calls] == [True, False]
    assert "Página 3 de 3" in retry.calls[0][0][0]["content"]


def test_checkpoint_for_another_pdf_is_rejected_and_page_reprocessed():
    store = MemoryPageCheckpointStore()
    process_pdf("a.pdf", make_pdf(1, "Documento A"), "", FakeOllama(), job_id=store.job_id,
                checkpoint_store=store)

    retry = FakeOllama()
    process_pdf("b.pdf", make_pdf(1, "Documento B"), "", retry, job_id=store.job_id,
                checkpoint_store=store)

    assert sum(image is not None for _messages, image in retry.calls) == 1


def test_corrupt_or_schema_invalid_checkpoint_is_reprocessed(caplog):
    from app.document_service import _checkpoint_fingerprint, PAGE_EXTRACTION_SCHEMA_VERSION
    from app.schemas import PageExtraction

    store = MemoryPageCheckpointStore()
    pdf = make_pdf(1)
    # Identity matches, but extraction fails current Pydantic validation.
    import hashlib
    settings_value = get_settings()
    # Use the production fingerprint helper as the checkpoint identity.
    pipeline_hash = _checkpoint_fingerprint(settings_value)
    store.objects[f"{store.prefix}page-0001/corrupt.json"] = "{"
    store.objects[f"{store.prefix}page-0001/old-owner.json"] = {
        "checkpoint_version": 1,
        "job_id": store.job_id,
        "page_number": 1,
        "total_pages": 1,
        "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
        "schema_version": PAGE_EXTRACTION_SCHEMA_VERSION,
        "pipeline_fingerprint": pipeline_hash,
        "context_summary_sha256": hashlib.sha256(b"").hexdigest(),
        "extraction": {"transcription": {"private": "bad type"}},
    }
    bad_key = f"{store.prefix}page-0001/old-owner.json"
    store.manifest = {
        "manifest_version": 1,
        "job_id": store.job_id,
        "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
        "total_pages": 1,
        "schema_version": PAGE_EXTRACTION_SCHEMA_VERSION,
        "pipeline_fingerprint": pipeline_hash,
        "pages": {"1": {"key": bad_key, "sha256": hashlib.sha256(json.dumps(
            store.objects[bad_key], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()}},
    }
    fake = FakeOllama()
    with caplog.at_level(logging.WARNING, logger="app.document_service"):
        process_pdf("synthetic.pdf", pdf, "", fake, job_id=store.job_id, checkpoint_store=store)

    assert sum(image is not None for _messages, image in fake.calls) == 1
    assert "CHECKPOINT_INVALID" in caplog.text
    assert "private" not in caplog.text


def test_checkpoint_save_failure_does_not_advance_page_progress():
    store = MemoryPageCheckpointStore()
    store.fail_save = True
    progress = []

    with pytest.raises(RuntimeError, match="checkpoint write failed"):
        process_pdf(
            "synthetic.pdf", make_pdf(2), "", FakeOllama(),
            lambda current, total: progress.append((current, total)),
            job_id=store.job_id,
            checkpoint_store=store,
        )

    assert progress == [(0, 2)]
    assert store.objects == {}


def test_final_synthesis_retry_reuses_all_page_checkpoints():
    store = MemoryPageCheckpointStore()
    pdf = make_pdf(1)
    failed = FakeOllama(responses=[valid_page_output(), "bad synthesis", "still bad synthesis"])

    with pytest.raises(OllamaError):
        process_pdf("synthetic.pdf", pdf, "", failed, job_id=store.job_id,
                    checkpoint_store=store)

    assert len(store.objects) == 1
    retry = FakeOllama(responses=[valid_final_output()])
    result = process_pdf("synthetic.pdf", pdf, "", retry, job_id=store.job_id,
                         checkpoint_store=store)

    assert len(retry.calls) == 1
    assert retry.calls[0][1] is None
    assert '"page": 1' in retry.calls[0][0][0]["content"]
    assert result["pages"] == 1


def test_truncated_final_synthesis_preserves_pages_and_retry_repeats_only_synthesis():
    store = MemoryPageCheckpointStore()
    pdf = make_pdf(1)
    truncated = OllamaOutput(
        valid_final_output(),
        {"done": True, "done_reason": "length", "eval_count": 2000},
    )
    first_delivery = FakeOllama(responses=[valid_page_output(), truncated])

    with pytest.raises(OllamaError) as error:
        process_pdf("synthetic.pdf", pdf, "", first_delivery,
                    job_id=store.job_id, checkpoint_store=store)

    assert error.value.category == "output_limit_reached"
    assert len(store.manifest["pages"]) == 1
    checkpoint_objects = dict(store.objects)

    retry = FakeOllama()
    result = process_pdf("synthetic.pdf", pdf, "", retry,
                         job_id=store.job_id, checkpoint_store=store)

    assert [image for _messages, image in retry.calls] == [None]
    assert store.objects.keys() >= checkpoint_objects.keys()
    assert result["pages"] == 1


def test_s3_checkpoint_store_writes_unique_objects_and_cleans_only_job_prefix():
    class S3:
        def __init__(self):
            self.objects = {}
            self.deleted = []

        def put_object(self, **kwargs):
            self.objects[kwargs["Key"]] = {
                "Body": kwargs["Body"],
                "Metadata": kwargs["Metadata"],
            }

        def head_object(self, **kwargs):
            stored = self.objects[kwargs["Key"]]
            return {"ContentLength": len(stored["Body"]), "Metadata": stored["Metadata"]}

        def list_objects_v2(self, **kwargs):
            contents = [{"Key": key} for key in self.objects if key.startswith(kwargs["Prefix"])]
            return {"Contents": contents, "IsTruncated": False}

        def get_object(self, **kwargs):
            body = self.objects[kwargs["Key"]]["Body"]
            return {"Body": type("Body", (), {"read": lambda _self: body})()}

        def delete_object(self, **kwargs):
            self.deleted.append(kwargs["Key"])
            self.objects.pop(kwargs["Key"], None)

    s3 = S3()
    store = S3PageCheckpointStore(s3, "results", "job-a", "owner-a")
    payload = {
        "job_id": "job-a",
        "page_number": 1,
        "total_pages": 1,
        "pdf_sha256": "a" * 64,
        "schema_version": "page-extraction-v1",
        "pipeline_fingerprint": "b" * 64,
        "extraction": {},
    }
    first_key, _ = store.save(1, payload)
    second_key, _ = store.save(1, payload)
    assert first_key != second_key
    assert store.load(first_key) == payload
    assert store.load_manifest() == store.manifest
    loaded, loaded_key = store.load_checkpoint(store.manifest["pages"]["1"])
    assert loaded == payload
    assert loaded_key == second_key
    s3.objects["checkpoints/job-b/page-0001/other.json"] = {"Body": b"{}", "Metadata": {}}

    assert store.delete_all() == 4
    assert "checkpoints/job-b/page-0001/other.json" in s3.objects
