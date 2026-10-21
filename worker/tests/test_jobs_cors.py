import json

import pytest

from app import jobs


FRONTEND_ORIGIN = "https://dfw9n3vcykvho.cloudfront.net"


def _event(method, path):
    return {
        "httpMethod": method,
        "resource": path,
        "pathParameters": {"job_id": "job-123"},
        "requestContext": {"authorizer": {"claims": {"sub": "cognito-sub"}}},
        "body": "{}",
    }


def _assert_cors(response):
    headers = response["headers"]
    assert headers["Access-Control-Allow-Origin"] == FRONTEND_ORIGIN
    assert headers["Access-Control-Allow-Origin"] != "*"
    assert headers["Access-Control-Allow-Methods"] == "GET,POST,DELETE,OPTIONS"
    allowed_headers = {part.lower() for part in headers["Access-Control-Allow-Headers"].split(",")}
    assert {"authorization", "content-type"}.issubset(allowed_headers)


@pytest.mark.parametrize(
    ("method", "path", "handler_name"),
    [
        ("POST", "/jobs", "create_job"),
        ("GET", "/jobs", "list_jobs"),
        ("GET", "/jobs/{job_id}", "get_job"),
        ("DELETE", "/jobs/{job_id}", "delete_job"),
    ],
)
def test_protected_routes_return_configured_cors_origin(monkeypatch, method, path, handler_name):
    monkeypatch.setenv("CORS_ORIGIN", FRONTEND_ORIGIN)
    monkeypatch.setattr(jobs, handler_name, lambda *_args: jobs._response(200, {"ok": True}))

    response = jobs.lambda_handler(_event(method, path), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"ok": True}
    _assert_cors(response)


def test_options_preflight_returns_configured_origin_without_auth(monkeypatch):
    monkeypatch.setenv("CORS_ORIGIN", FRONTEND_ORIGIN)
    event = {"httpMethod": "OPTIONS", "resource": "/jobs"}

    response = jobs.lambda_handler(event, None)

    assert response["statusCode"] == 204
    _assert_cors(response)


def test_api_gateway_cors_uses_same_configured_origin_and_allows_auth_header():
    from pathlib import Path

    template = Path(__file__).resolve().parents[2] / "infrastructure" / "aws" / "stacks" / "api.yml"
    source = template.read_text()

    assert "AllowOrigin: !Sub \"'${FrontendOrigin}'\"" in source
    assert "AllowMethods: \"'GET,POST,DELETE,OPTIONS'\"" in source
    assert "AllowHeaders: \"'Content-Type,Authorization'\"" in source
    assert "AddDefaultAuthorizerToCorsPreflight: false" in source
    assert "Access-Control-Allow-Origin: !Sub \"'${FrontendOrigin}'\"" in source
    assert "Access-Control-Allow-Origin: '*'" not in source

