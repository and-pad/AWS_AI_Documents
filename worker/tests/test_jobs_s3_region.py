from app import jobs


def test_presigned_s3_url_uses_lambda_region(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")

    client = jobs._create_s3_client()
    upload_url = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": "test-documents-bucket", "Key": "users/example/jobs/123/file.pdf"},
        ExpiresIn=900,
    )

    assert upload_url.startswith(
        "https://test-documents-bucket.s3.us-east-2.amazonaws.com/"
    )


def test_s3_client_preserves_localstack_endpoint_environment(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")

    client = jobs._create_s3_client()

    assert client.meta.endpoint_url == "http://localhost:4566"
