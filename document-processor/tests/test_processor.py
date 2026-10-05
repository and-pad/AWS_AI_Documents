import json

import fitz
import pytest
from fastapi.testclient import TestClient

from app import main
from app.document_service import process_pdf
from app.ollama_client import OllamaError
from app.settings import get_settings


class FakeOllama:
    def __init__(self, fail_on_call=None):
        self.calls = []
        self.fail_on_call = fail_on_call

    def chat(self, messages, image=None):
        self.calls.append((messages, image))
        if self.fail_on_call == len(self.calls):
            raise OllamaError("provider unavailable")
        if image is None:
            return "Consolidación final"
        page_number = len([call for call in self.calls if call[1] is not None])
        return json.dumps({
            "analysis": f"Análisis {page_number}",
            "context_summary": f"Documento; página previa {page_number}",
        })


def make_pdf(page_count=1):
    document = fitz.open()
    for page_number in range(page_count):
        page = document.new_page()
        page.insert_text((72, 72), f"Contenido visible {page_number + 1}")
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


def test_one_page_and_empty_prompt(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr("app.document_service.OllamaClient", lambda: fake)
    result = process_pdf("one.pdf", make_pdf(), "", None)
    assert result["pages"] == 1
    assert result["page_results"] == [{"page": 1, "analysis": "Análisis 1"}]
    assert result["final_analysis"] == "Consolidación final"
    assert result["user_prompt"] == ""
    assert fake.calls[-1][1] is None


def test_multipage_sequential_context_and_prompt(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr("app.document_service.OllamaClient", lambda: fake)
    result = process_pdf("many.pdf", make_pdf(3), "Identifica cantidades", None)
    assert result["pages"] == 3
    assert [item["page"] for item in result["page_results"]] == [1, 2, 3]
    assert result["user_prompt"] == "Identifica cantidades"
    assert "Identifica cantidades" in fake.calls[0][0][0]["content"]
    assert "página previa 1" in fake.calls[1][0][0]["content"]
    assert "página previa 2" in fake.calls[2][0][0]["content"]
    assert fake.calls[-1][1] is None


def test_endpoint_accepts_user_prompt(monkeypatch):
    expected = {"filename": "document.pdf", "pages": 1, "page_results": [], "final_analysis": "ok", "user_prompt": "Resume"}
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


def test_invalid_pdf_is_rejected():
    response = TestClient(main.app).post("/process", files={"file": ("invalid.pdf", b"not a pdf", "application/pdf")})
    assert response.status_code == 400
