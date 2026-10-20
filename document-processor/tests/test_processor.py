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


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "2000000")
    monkeypatch.setenv("PDF_RENDER_DPI", "72")
    yield
    get_settings.cache_clear()


def test_health():
    response = TestClient(main.app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
    response = type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {"message": {"content": "ok"}}})()
    calls = []
    monkeypatch.setattr("app.ollama_client.requests.post", lambda *args, **kwargs: (calls.append((args, kwargs)) or response))

    assert OllamaClient().chat([{"role": "user", "content": "hello"}]) == "ok"
    assert OllamaClient().url == "https://la-ea.duckdns.org:11443"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer unit-test-token"}


def test_ollama_client_sends_json_schema_as_format(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_TOKEN", raising=False)
    get_settings.cache_clear()
    response = type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {"message": {"content": "{}"}}})()
    calls = []
    monkeypatch.setattr("app.ollama_client.requests.post", lambda *args, **kwargs: (calls.append(kwargs) or response))
    schema = {"type": "object", "required": ["answer"]}

    OllamaClient().chat([{"role": "user", "content": "json"}], format=schema)

    assert calls[0]["json"]["format"] == schema


def test_ollama_client_forwards_retry_generation_options(monkeypatch):
    get_settings.cache_clear()
    response = type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {"message": {"content": "{}"}}})()
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
    }


def test_ollama_client_allows_missing_optional_metadata(monkeypatch):
    monkeypatch.setattr(
        "app.ollama_client.requests.post",
        lambda *args, **kwargs: FakeHttpResponse({"message": {"role": "assistant", "content": "{}"}}),
    )

    output = OllamaClient().chat([{"role": "user", "content": "page"}])

    assert output == "{}"
    assert output.metadata == {}


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
    assert "category=http_error" in caplog.text
    assert "status_code=503" in caplog.text
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
    assert "category=missing_content" in caplog.text
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
    response = type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {"message": {"content": "ok"}}})()
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
    assert client.options == [{"num_ctx": 16384}]


def test_invalid_json_retries_with_schema_and_same_sampling_then_accepts_valid_output(caplog):
    from app.schemas import PageExtraction

    client = FakeOllama(responses=["no es JSON confidencial", valid_page_output()])
    with caplog.at_level(logging.WARNING, logger="app.document_service"):
        result = _generate_validated(client, [{"role": "user", "content": "extraer"}], PageExtraction, image=b"image")

    assert result.transcription == "Línea uno\nLínea dos"
    assert len(client.calls) == 2
    assert client.formats[0] == client.formats[1] == PageExtraction.model_json_schema()
    assert client.options == [{"num_ctx": 16384}, {"num_ctx": 16384}]
    assert "no es JSON confidencial" not in client.calls[1][0][-1]["content"]
    assert "JSON Schema" in client.calls[1][0][-1]["content"]
    assert client.calls[1][1] == b"image"
    assert "kind=invalid_json error_type=JSONDecodeError" in caplog.text
    assert "position=" in caplog.text
    assert "no es JSON confidencial" not in caplog.text


def test_debug_model_output_is_disabled_by_default_and_never_logs_raw_content(monkeypatch, caplog):
    from app.schemas import PageExtraction

    monkeypatch.delenv("DEBUG_MODEL_OUTPUT", raising=False)
    get_settings.cache_clear()
    sensitive_raw = OllamaOutput(
        '{"transcription":"SECRET_DOCUMENT_TEXT"',
        {"done": True, "done_reason": "length", "eval_count": 17},
    )
    client = FakeOllama(responses=[sensitive_raw, valid_page_output()])

    with caplog.at_level(logging.WARNING, logger="app.document_service"):
        _generate_validated(client, [{"role": "user", "content": "private prompt"}], PageExtraction)

    assert "DEBUG_MODEL_OUTPUT" not in caplog.text
    assert "SECRET_DOCUMENT_TEXT" not in caplog.text
    assert "done_reason='length'" not in caplog.text
    assert "private prompt" not in caplog.text


def test_debug_model_output_logs_bounded_raw_fragments_and_safe_metadata(monkeypatch, caplog):
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
            "done_reason": "length",
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

    assert "DEBUG_MODEL_OUTPUT raw_length=1213" in caplog.text
    assert f"raw_first_500={repr(raw[:500])}" in caplog.text
    assert f"raw_last_500={repr(raw[-500:])}" in caplog.text
    assert "done=True" in caplog.text
    assert "done_reason='length'" in caplog.text
    assert "eval_count=321" in caplog.text
    assert "prompt_eval_count=123" in caplog.text
    assert "total_duration=987" in caplog.text
    assert "SECRET_MIDDLE" not in caplog.text
    assert token not in caplog.text
    assert "private prompt" not in caplog.text


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

    assert "ValidationError PageExtraction" in caplog.text
    assert "error_count=1" in caplog.text
    assert "path=visual_elements.stamps.0.visible_text" in caplog.text
    assert "type=string_type" in caplog.text
    assert "Input should be a valid string" in caplog.text
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
    assert fake.options == [{"num_ctx": 16384}] * 3
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


def test_structured_calls_configure_context_without_forcing_reasoning_or_token_cap(monkeypatch):
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
    assert captured[0]['options'] == {'num_ctx': 12288}
    assert captured[0]['truncate'] is False
    assert captured[0]['shift'] is False
    assert 'think' not in captured[0]
    assert 'num_predict' not in captured[0]['options']


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
    assert 'job_id=synthetic-job' in caplog.text
    assert 'image_sha256=' in caplog.text
    assert 'image_width=' in caplog.text
    assert 'extraction_count=1' in caplog.text
    assert 'PRIVATE REQUEST' not in caplog.text
    assert 'Encabezado 1' not in caplog.text
    from app.diagnostics import current_job_id
    assert current_job_id() == 'local'
