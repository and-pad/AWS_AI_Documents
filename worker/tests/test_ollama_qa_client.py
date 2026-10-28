import json
import logging
import socket

import pytest
import requests

from app import ollama_qa_client
from app.qa_service import QuestionContext, QuestionSection
from app.ollama_qa_client import QuestionModelError, ask_ollama


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.ok = status < 400
        self.headers = headers or {}

    def json(self):
        return self.payload


def context():
    return QuestionContext('[{"page":2,"kind":"transcripción","text":"Nombre: Ana"}]', (2,), False, 1)


def administrative_context():
    text = (
        "Oficio RH-17/2025. La remitente solicita al Departamento de Recursos Humanos informar "
        "el sueldo mensual de los servidores públicos mencionados."
    )
    return QuestionContext(
        json.dumps([{"page": 1, "transcription": text}], ensure_ascii=False), (1,), False, 1,
    )


def reply_context():
    text = (
        "OFICIO No. ADM-42/2025. Fecha: 12 de agosto de 2025. "
        "Remitente: Ana Torres, Directora de Administración. "
        "Destinatario: Titular de Recursos Humanos. "
        "Asunto: Solicitud de reporte de plazas vacantes. "
        "Solicita remitir el reporte antes del 30 de agosto de 2025. "
        "Atentamente, Ana Torres, Directora de Administración. "
        "Sello de recibido: documento ingresado. Anotación: urgente."
    )
    return QuestionContext(
        json.dumps([{"page": 1, "transcription": text}], ensure_ascii=False), (1,), False, 1,
    )


def test_question_call_uses_structured_output_and_separate_token_budget(monkeypatch):
    calls = []
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test:11443")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3-vl:8b-instruct")
    monkeypatch.setenv("OLLAMA_ASK_MODEL", "qwen3-vl:8b-instruct")
    monkeypatch.setenv("OLLAMA_ASK_NUM_CTX", "8192")
    monkeypatch.delenv("OLLAMA_ASK_NUM_PREDICT", raising=False)
    monkeypatch.setenv("OLLAMA_ASK_READ_TIMEOUT", "780")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or Response({
            "message": {"content": json.dumps({"answer": "Ana", "source_pages": [2]})},
            "model": "qwen3-vl:8b-instruct",
            "done": True,
            "done_reason": "stop",
        })
    ))

    answer = ask_ollama("¿Quién aparece?", context(), "private-token")

    assert answer == {"answer": "Ana", "source_pages": [2], "model": "qwen3-vl:8b-instruct", "context_mode": "FULL_TRANSCRIPT"}
    request = calls[0][1]
    assert request["timeout"] == (5, 780)
    assert request["headers"]["Authorization"] == "Bearer private-token"
    assert request["json"]["stream"] is False
    assert request["json"]["model"] == "qwen3-vl:8b-instruct"
    assert request["json"]["think"] is False
    assert request["json"]["options"] == {"num_ctx": 8192, "num_predict": 2000, "temperature": 0}
    assert request["headers"]["X-Request-ID"]
    assert request["json"]["format"]["additionalProperties"] is False


@pytest.mark.parametrize("question, answer, pages", [
    (
        "¿Cuál es el número del oficio?",
        "El número del oficio es RH-17/2025.",
        [1],
    ),
    (
        "Resume este documento.",
        "El oficio RH-17/2025 solicita al Departamento de Recursos Humanos información sobre el sueldo mensual de los servidores públicos mencionados.",
        [1],
    ),
    (
        "Explícame qué están solicitando.",
        "Solicitan que Recursos Humanos informe el sueldo mensual de los servidores públicos mencionados.",
        [1],
    ),
    (
        "Analiza qué respuesta puede darse con estos datos.",
        "El oficio pide información salarial, pero no incluye los datos salariales ni una respuesta autorizada; esos datos deben verificarse antes de contestar.",
        [1],
    ),
    (
        "Ayúdame a contestar este oficio.",
        "Borrador: En atención al oficio RH-17/2025, acusamos recibo de la solicitud de información sobre sueldos. [DATO FALTANTE: respuesta verificada y plazo de entrega].",
        [1],
    ),
    (
        "Hazme un machote para contestar este oficio.",
        "[Lugar y fecha]\n\nA la persona remitente del oficio RH-17/2025\nAsunto: Respuesta a solicitud de información\n\nEn atención al oficio RH-17/2025, mediante el cual solicita información sobre el sueldo mensual de los servidores públicos mencionados, comunico que [DATO FALTANTE: respuesta institucional autorizada].\n\nAtentamente,\n[DATO FALTANTE: nombre y cargo de quien firma]",
        [1],
    ),
    (
        "Redacta una respuesta formal al remitente.",
        "Borrador: En atención al oficio RH-17/2025, relativo a la solicitud de información salarial, se informa que [DATO FALTANTE: respuesta autorizada]. Atentamente, [DATO FALTANTE: nombre y cargo de quien firma].",
        [1],
    ),
    (
        "¿Cuánto gana una persona mencionada si su sueldo no aparece en el documento?",
        "El documento no especifica el sueldo de la persona mencionada, así que no es posible determinarlo con esta información.",
        [],
    ),
])
def test_document_tasks_return_grounded_answers_or_explicit_missing_data(monkeypatch, question, answer, pages):
    calls = []
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or Response({
            "model": "qwen3-vl:8b-instruct",
            "message": {"content": json.dumps({"answer": answer, "source_pages": pages}, ensure_ascii=False)},
            "done": True,
            "done_reason": "stop",
        })
    ))

    result = ask_ollama(question, administrative_context(), "")

    assert result["answer"] == answer
    assert result["source_pages"] == pages
    messages = calls[0][1]["json"]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    system_prompt = " ".join(messages[0]["content"].split())
    assert "La tarea principal es cumplir la solicitud del usuario." in system_prompt
    assert "Nunca inventes nombres, fechas, cantidades, salarios" in system_prompt
    assert question in messages[1]["content"]
    if any(word in question.casefold() for word in ("contestar", "machote", "redacta")):
        assert "entrega directamente el borrador solicitado" in system_prompt
    if "machote" in question:
        assert result["answer"].startswith("[Lugar y fecha]")
        assert "[DATO FALTANTE: respuesta institucional autorizada]" in result["answer"]
        assert "[DATO FALTANTE: nombre y cargo de quien firma]" in result["answer"]
        assert "El documento trata sobre" not in result["answer"]
    if "sueldo no aparece" in question:
        assert "$" not in result["answer"]
        assert result["source_pages"] == []


def test_reply_machote_uses_known_original_metadata_and_only_marks_new_unknowns(monkeypatch):
    question = "Puedes ayudarme con una respuesta a este documento, un machote esperando solo los datos que no puedes saber."
    draft = (
        "[Fecha de respuesta: DATO FALTANTE: fecha de emisión del nuevo documento]\n\n"
        "OFICIO No. [DATO FALTANTE: número del nuevo oficio]\n"
        "De: Titular de Recursos Humanos\n"
        "A: Ana Torres, Directora de Administración\n"
        "Asunto: Respuesta al oficio ADM-42/2025\n\n"
        "En atención a su oficio ADM-42/2025, de fecha 12 de agosto de 2025, relativo a la solicitud de un reporte "
        "de plazas vacantes con fecha límite del 30 de agosto de 2025, informo que "
        "[DATO FALTANTE: respuesta o estado institucional del reporte].\n\n"
        "Atentamente,\n[DATO FALTANTE: nombre y cargo de quien suscribe]"
    )
    calls = []
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *args, **kwargs: (
        calls.append(kwargs) or Response({
            "model": "qwen3-vl:8b-instruct",
            "message": {"content": json.dumps({"answer": draft, "source_pages": [1]}, ensure_ascii=False)},
            "done": True,
            "done_reason": "stop",
        })
    ))

    result = ask_ollama(question, reply_context(), "")

    assert result["answer"] == draft
    assert result["source_pages"] == [1]
    received_document = json.loads(reply_context().text)[0]["transcription"]
    assert received_document not in result["answer"]
    assert "ADM-42/2025" in draft
    assert "12 de agosto de 2025" in draft
    assert "Ana Torres, Directora de Administración" in draft
    assert "DATO FALTANTE: número del nuevo oficio" in draft
    assert "DATO FALTANTE: número del oficio original" not in draft
    assert "DATO FALTANTE: fecha del documento recibido" not in draft
    assert "Sello de recibido" not in draft and "Anotación: urgente" not in draft
    assert "fecha límite del 30 de agosto" in draft
    assert "se entregó" not in draft.casefold() and "se aprobó" not in draft.casefold()
    assert "[p. 1]" not in draft and "página 1" not in draft.casefold()

    messages = calls[0]["json"]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    system_prompt = " ".join(messages[0]["content"].split())
    assert "crea una comunicación nueva" in system_prompt
    assert "el número y la fecha originales como referencia" in system_prompt
    assert "No marques como faltante ningún dato que aparezca en la transcripción." in system_prompt
    assert "no copies su cuerpo como si fuera la contestación" in system_prompt
    assert "no insertes referencias a páginas dentro del texto" in system_prompt
    assert question in messages[1]["content"]


def test_uncited_salary_is_rejected_even_if_answer_says_the_value_is_missing(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_args, **_kwargs: Response({
        "model": "qwen3-vl:8b-instruct",
        "message": {"content": json.dumps({
            "answer": "El documento no especifica el sueldo, pero la persona gana $50,000.",
            "source_pages": [],
        })},
        "done": True,
        "done_reason": "stop",
    }))

    with pytest.raises(QuestionModelError) as failure:
        ask_ollama(
            "¿Cuánto gana una persona mencionada si su sueldo no aparece en el documento?",
            administrative_context(),
            "",
        )

    assert failure.value.category == "invalid_answer"


def test_hierarchical_no_quote_still_runs_final_drafting_synthesis(monkeypatch):
    text = "El documento pide un informe cuyo contenido debe verificarse internamente."
    section = QuestionSection(
        text=json.dumps([{"page": 1, "transcription": text}], ensure_ascii=False),
        page_numbers=(1,),
        page_texts=((1, text),),
    )
    hierarchical = QuestionContext(
        text=section.text,
        page_numbers=(1,),
        context_limited=True,
        source_chunk_count=1,
        context_mode="HIERARCHICAL_FULL_TRANSCRIPT",
        sections=(section,),
    )
    calls = []
    replies = iter([
        Response({
            "model": "qwen3-vl:8b-instruct",
            "message": {"content": '{"evidence":[]}'},
            "done": True,
            "done_reason": "stop",
        }),
        Response({
            "model": "qwen3-vl:8b-instruct",
            "message": {"content": json.dumps({
                "answer": "Borrador: En atención a su solicitud, [DATO FALTANTE: contenido del informe verificado].",
                "source_pages": [],
            }, ensure_ascii=False)},
            "done": True,
            "done_reason": "stop",
        }),
    ])
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or next(replies)
    ))

    result = ask_ollama("Ayúdame a contestar este documento.", hierarchical, "")

    assert result["answer"].startswith("Borrador:")
    assert result["source_pages"] == []
    assert len(calls) == 2
    evidence_messages = calls[0][1]["json"]["messages"]
    final_messages = calls[1][1]["json"]["messages"]
    assert "La tarea principal sigue siendo cumplir la solicitud original del usuario." in evidence_messages[0]["content"]
    assert "[DATO FALTANTE:" in final_messages[0]["content"]
    assert "\n[]" in final_messages[1]["content"]


def test_hierarchical_machote_keeps_original_task_in_every_map_and_synthesis_call(monkeypatch):
    question = "Puedes ayudarme con una respuesta a este documento, un machote esperando solo los datos que no puedes saber."
    first_text = (
        "OFICIO No. ADM-42/2025. Fecha: 12 de agosto de 2025. "
        "Remitente: Ana Torres, Directora de Administración. "
        "Destinatario: Titular de Recursos Humanos."
    )
    second_text = (
        "Asunto: Solicitud de reporte de plazas vacantes. "
        "Solicita remitir el reporte antes del 30 de agosto de 2025. "
        "Atentamente, Ana Torres, Directora de Administración."
    )
    sections = (
        QuestionSection(
            text=json.dumps([{"page": 1, "transcription": first_text}], ensure_ascii=False),
            page_numbers=(1,),
            page_texts=((1, first_text),),
        ),
        QuestionSection(
            text=json.dumps([{"page": 2, "transcription": second_text}], ensure_ascii=False),
            page_numbers=(2,),
            page_texts=((2, second_text),),
        ),
    )
    hierarchical = QuestionContext(
        text=json.dumps([
            {"page": 1, "transcription": first_text},
            {"page": 2, "transcription": second_text},
        ], ensure_ascii=False),
        page_numbers=(1, 2),
        context_limited=True,
        source_chunk_count=len(sections),
        context_mode="HIERARCHICAL_FULL_TRANSCRIPT",
        sections=sections,
    )
    draft = (
        "[Fecha de respuesta: DATO FALTANTE: fecha de emisión del nuevo documento]\n\n"
        "OFICIO No. [DATO FALTANTE: número del nuevo oficio]\n"
        "De: Titular de Recursos Humanos\n"
        "A: Ana Torres, Directora de Administración\n"
        "Asunto: Respuesta al oficio ADM-42/2025\n\n"
        "En atención a su oficio ADM-42/2025, de fecha 12 de agosto de 2025, relativo a la solicitud de un reporte "
        "de plazas vacantes con fecha límite del 30 de agosto de 2025, informo que "
        "[DATO FALTANTE: respuesta o estado institucional del reporte].\n\n"
        "Atentamente,\n[DATO FALTANTE: nombre y cargo de quien suscribe]"
    )
    replies = iter([
        Response({
            "model": "qwen3-vl:8b-instruct",
            "message": {"content": json.dumps({"evidence": [
                {"page": 1, "quote": "OFICIO No. ADM-42/2025. Fecha: 12 de agosto de 2025."},
                {"page": 1, "quote": "Remitente: Ana Torres, Directora de Administración. Destinatario: Titular de Recursos Humanos."},
            ]}, ensure_ascii=False)},
            "done": True,
            "done_reason": "stop",
        }),
        Response({
            "model": "qwen3-vl:8b-instruct",
            "message": {"content": json.dumps({"evidence": [
                {"page": 2, "quote": "Asunto: Solicitud de reporte de plazas vacantes."},
                {"page": 2, "quote": "Solicita remitir el reporte antes del 30 de agosto de 2025."},
                {"page": 2, "quote": "Atentamente, Ana Torres, Directora de Administración."},
            ]}, ensure_ascii=False)},
            "done": True,
            "done_reason": "stop",
        }),
        Response({
            "model": "qwen3-vl:8b-instruct",
            "message": {"content": json.dumps({"answer": draft, "source_pages": [1, 2]}, ensure_ascii=False)},
            "done": True,
            "done_reason": "stop",
        }),
    ])
    calls = []
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *args, **kwargs: (
        calls.append(kwargs) or next(replies)
    ))

    result = ask_ollama(question, hierarchical, "")

    assert result["answer"] == draft
    assert result["source_pages"] == [1, 2]
    assert first_text not in result["answer"] and second_text not in result["answer"]
    assert "Ana Torres, Directora de Administración" in result["answer"]
    assert "ADM-42/2025" in result["answer"]
    assert "12 de agosto de 2025" in result["answer"]
    assert "De: Titular de Recursos Humanos" in result["answer"]
    assert "DATO FALTANTE: número del nuevo oficio" in result["answer"]
    assert "DATO FALTANTE: número del oficio original" not in result["answer"]
    assert "DATO FALTANTE: fecha del oficio recibido" not in result["answer"]
    assert "30 de agosto de 2025" in result["answer"]
    assert "se entregó" not in result["answer"].casefold()
    assert "se aprobó" not in result["answer"].casefold()
    assert "[p. 1]" not in result["answer"] and "[p. 2]" not in result["answer"]
    assert len(calls) == 3
    for request in calls:
        messages = request["json"]["messages"]
        assert [message["role"] for message in messages] == ["system", "user"]
        assert question in messages[1]["content"]
    for evidence_request in calls[:2]:
        evidence_prompt = " ".join(evidence_request["json"]["messages"][0]["content"].split())
        assert "La tarea principal sigue siendo cumplir la solicitud original del usuario." in evidence_prompt
        assert "número y fecha originales" in evidence_prompt
        assert "no confundas esos datos con el número, fecha, remitente ni decisión del nuevo documento" in evidence_prompt
    final_messages = calls[-1]["json"]["messages"]
    final_prompt = " ".join(final_messages[0]["content"].split())
    assert "La tarea principal es cumplir la solicitud del usuario." in final_prompt
    assert "no sustituyas una tarea de redacción o análisis por una descripción del documento" in final_prompt
    assert "A: Ana Torres, Directora de Administración" in result["answer"]
    assert "source_pages" in final_prompt and "no insertes referencias a páginas dentro del texto" in final_prompt
    assert "ADM-42/2025" in final_messages[1]["content"]
    assert "Ana Torres, Directora de Administración" in final_messages[1]["content"]
    assert "[Fecha de respuesta: DATO FALTANTE:" in result["answer"]


@pytest.mark.parametrize("exception, category", [
    (requests.ReadTimeout("timeout"), "read_timeout"),
    (requests.ConnectionError("down"), "connection_error"),
    (requests.exceptions.SSLError("certificate invalid"), "tls_error"),
    (requests.ConnectionError("dns", socket.gaierror(-2, "private-host")), "dns_error"),
])
def test_transport_errors_are_classified_without_logging_private_content(monkeypatch, caplog, exception, category):
    token, question = "secret-token-value", "pregunta privada"
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_args, **_kwargs: (_ for _ in ()).throw(exception))
    with caplog.at_level(logging.INFO), pytest.raises(QuestionModelError) as error:
        ask_ollama(question, context(), token)
    assert error.value.category == category
    assert token not in caplog.text
    assert question not in caplog.text
    assert token not in str(error.value)


@pytest.mark.parametrize("status", [500, 502, 504])
def test_http_gateway_failures_preserve_status_and_are_retryable(monkeypatch, status):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({}, status=status))
    with pytest.raises(QuestionModelError) as failure:
        ask_ollama("private question", context(), "secret")
    assert failure.value.category == "http_error"
    assert failure.value.http_status == status
    assert failure.value.retryable is True


def test_ollama_api_error_envelope_is_classified_without_logging_its_payload(monkeypatch, caplog):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({"error": "private backend detail"}))

    with pytest.raises(QuestionModelError) as failure:
        ask_ollama("private question", context(), "secret-token")

    assert failure.value.category == "ollama_api_error"
    assert failure.value.retryable is True
    assert "private backend detail" not in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize("status", [401, 403])
def test_bad_bearer_is_permanent_without_echoing_token(monkeypatch, caplog, status):
    secret = "do-not-log-token"
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({}, status=status))
    with pytest.raises(QuestionModelError) as failure:
        ask_ollama("private question", context(), secret)
    assert failure.value.category == "authentication_error"
    assert failure.value.http_status == status
    assert failure.value.retryable is False
    assert secret not in caplog.text
    assert secret not in str(failure.value)


def test_json_and_truncation_errors_do_not_return_unvalidated_answers(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setenv("OLLAMA_ASK_NUM_PREDICT", "1000")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "message": {"content": '{"answer":'}, "done": True, "done_reason": "stop",
    }))
    with pytest.raises(QuestionModelError) as invalid_json:
        ask_ollama("pregunta", context(), "")
    assert invalid_json.value.category == "invalid_json"
    assert invalid_json.value.retryable is True

    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "message": {"content": '{"answer":'}, "done": True, "done_reason": "length",
        "eval_count": 1000,
    }))
    with pytest.raises(QuestionModelError) as incomplete:
        ask_ollama("pregunta", context(), "")
    assert incomplete.value.category == "invalid_json"
    assert incomplete.value.retryable is True


def test_http_server_error_can_be_retried_but_invalid_page_reference_cannot(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({}, status=503))
    with pytest.raises(QuestionModelError) as server_error:
        ask_ollama("pregunta", context(), "")
    assert server_error.value.retryable is True

    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "message": {"content": '{"answer":"Otra página","source_pages":[3]}'}, "done": True,
    }))
    with pytest.raises(QuestionModelError) as invalid_page:
        ask_ollama("pregunta", context(), "")
    assert invalid_page.value.category == "invalid_answer"
    assert invalid_page.value.retryable is True


def test_coordinator_busy_respects_retry_after_without_exposing_details(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response(
        {"error": "coordinator_busy", "retryable": True}, status=503, headers={"Retry-After": "600"},
    ))
    with pytest.raises(QuestionModelError) as failure:
        ask_ollama("pregunta privada", context(), "secret")
    assert failure.value.category == "coordinator_busy"
    assert failure.value.retry_after_seconds == 600
    assert failure.value.retryable is True
    assert "pregunta privada" not in str(failure.value)
    assert "secret" not in str(failure.value)


def test_thinking_markers_are_removed_before_parsing_or_returning(monkeypatch, caplog):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "model": "qwen3-vl:8b-instruct",
        "message": {"content": '<think>private reasoning must not be shown</think>\n{"answer":"Ana","source_pages":[2]}'},
        "done": True,
        "done_reason": "stop",
    }))
    with caplog.at_level(logging.INFO):
        answer = ask_ollama("pregunta privada", context(), "secret")
    assert answer["answer"] == "Ana"
    assert "reasoning" not in answer["answer"]
    assert "private reasoning" not in caplog.text
    assert "pregunta privada" not in caplog.text
    assert "secret" not in caplog.text


def test_separate_thinking_and_content_returns_only_final_content(monkeypatch, caplog):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "model": "qwen3-vl:8b-instruct",
        "message": {
            "thinking": "private reasoning must never be returned",
            "content": '{"answer":"Ana","source_pages":[2]}',
        },
        "done": True,
        "done_reason": "stop",
        "eval_count": 48,
        "total_duration": 123456,
    }))

    with caplog.at_level(logging.INFO):
        answer = ask_ollama("pregunta", context(), "")

    assert answer["answer"] == "Ana"
    assert answer["source_pages"] == [2]
    assert "private reasoning" not in caplog.text
    assert "thinking_present=True" in caplog.text
    assert "thinking_length=40" in caplog.text
    assert "done_reason=stop" in caplog.text
    assert "eval_count=48" in caplog.text


def test_thinking_without_final_content_is_rejected_and_logs_safe_metadata(monkeypatch, caplog):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "model": "qwen3-vl:8b-instruct",
        "message": {"thinking": "private reasoning that is not the answer", "content": ""},
        "done": True,
        "done_reason": "stop",
        "eval_count": 1400,
        "prompt_eval_count": 650,
        "total_duration": 123456,
        "eval_duration": 100000,
    }))

    with caplog.at_level(logging.INFO), pytest.raises(QuestionModelError) as failure:
        ask_ollama("private question", context(), "private-token")

    assert failure.value.category == "thinking_without_final_content"
    assert failure.value.retryable is True
    assert "ASK_OLLAMA_RESPONSE_RECEIVED" in caplog.text
    assert "response_content_length=0" in caplog.text
    assert "thinking_present=True" in caplog.text
    assert "thinking_length=40" in caplog.text
    assert "done_reason=stop" in caplog.text
    assert "eval_count=1400" in caplog.text
    assert "prompt_eval_count=650" in caplog.text
    assert "private reasoning" not in caplog.text
    assert "private question" not in caplog.text
    assert "private-token" not in caplog.text


def test_output_budget_exhaustion_is_not_retried_with_identical_settings(monkeypatch, caplog):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setenv("OLLAMA_ASK_NUM_PREDICT", "2000")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "model": "qwen3-vl:8b-instruct",
        "message": {"thinking": "reasoning", "content": ""},
        "done": True,
        "done_reason": "length",
        "eval_count": 2000,
        "eval_count": 2000,
    }))

    with caplog.at_level(logging.INFO), pytest.raises(QuestionModelError) as failure:
        ask_ollama("pregunta", context(), "")

    assert failure.value.category == "output_budget_exhausted"
    assert failure.value.retryable is False
    assert "ASK_OLLAMA_OUTPUT_LIMIT_REACHED" in caplog.text
    assert "num_predict=2000" in caplog.text
    assert "eval_count=2000" in caplog.text
    assert "reasoning" not in caplog.text


def test_output_limit_with_valid_content_is_still_returned(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setenv("OLLAMA_ASK_NUM_PREDICT", "2000")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "model": "qwen3-vl:8b-instruct",
        "message": {"thinking": "private reasoning", "content": '{"answer":"Ana","source_pages":[2]}'},
        "done": True,
        "done_reason": "length",
        "eval_count": 2000,
    }))

    answer = ask_ollama("pregunta", context(), "")
    assert answer["answer"] == "Ana"
    assert answer["source_pages"] == [2]


@pytest.mark.parametrize("done_reason, eval_count", [("length", 1999), ("stop", 2000), ("length", None)])
def test_empty_content_only_fails_terminally_when_length_reaches_budget(monkeypatch, done_reason, eval_count):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setenv("OLLAMA_ASK_NUM_PREDICT", "2000")
    payload = {
        "model": "qwen3-vl:8b-instruct",
        "message": {"thinking": "reasoning", "content": ""},
        "done": True,
        "done_reason": done_reason,
    }
    if eval_count is not None:
        payload["eval_count"] = eval_count
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response(payload))

    with pytest.raises(QuestionModelError) as failure:
        ask_ollama("pregunta", context(), "")
    assert failure.value.category == "thinking_without_final_content"
    assert failure.value.retryable is True


def test_invalid_json_is_logged_without_response_content(monkeypatch, caplog):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "model": "qwen3-vl:8b-instruct",
        "message": {"content": '{"answer":"sensitive partial text'},
        "done": True,
        "done_reason": "stop",
    }))

    with caplog.at_level(logging.INFO), pytest.raises(QuestionModelError) as failure:
        ask_ollama("pregunta", context(), "")

    assert failure.value.category == "invalid_json"
    assert "ASK_OLLAMA_RESPONSE_JSON_INVALID" in caplog.text
    assert "json_valid=false" in caplog.text
    assert "sensitive partial text" not in caplog.text


def test_unclosed_thinking_is_rejected_as_incomplete(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "model": "qwen3-vl:8b-instruct",
        "message": {"content": "<think>reasoning without a final answer"},
        "done": True,
        "done_reason": "stop",
    }))
    with pytest.raises(QuestionModelError) as failure:
        ask_ollama("pregunta", context(), "")
    assert failure.value.category == "incomplete_reasoning"
    assert failure.value.retryable is True


def test_wrong_model_envelope_is_rejected(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.test")
    monkeypatch.setattr(ollama_qa_client.requests, "post", lambda *_a, **_k: Response({
        "model": "deepseek-r1:8b",
        "message": {"content": '{"answer":"Ana","source_pages":[2]}'},
        "done": True,
    }))
    with pytest.raises(QuestionModelError) as failure:
        ask_ollama("pregunta", context(), "")
    assert failure.value.category == "model_mismatch"
