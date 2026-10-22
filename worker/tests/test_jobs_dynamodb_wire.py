import json

import boto3
from botocore.awsrequest import AWSResponse

from app import jobs


class FakeResponseBody:
    def stream(self, _amount=1024, decode_content=False):
        return iter([b"{}"])


class QuotaTable:
    name = "jobs-table"

    def __init__(self, count):
        self.active_count = count

    def get_item(self, *, Key, **_kwargs):
        if Key["job_id"] == jobs._quota_key("cognito-sub"):
            return {"Item": {"job_id": Key["job_id"], "active_count": self.active_count}}
        return {}

    def query(self, **_kwargs):
        return {"Count": self.active_count}

    def put_item(self, *, Item, **_kwargs):
        self.active_count = Item["active_count"]
        return {}


def install_wire_capture(monkeypatch, table=None):
    resource = boto3.resource(
        "dynamodb",
        region_name="us-east-2",
        aws_access_key_id="unit-test-access-key",
        aws_secret_access_key="unit-test-secret-key",
    )
    wire_requests = []

    def capture(request, **_kwargs):
        body = json.loads(request.body)
        wire_requests.append(body)
        if table is not None:
            update = body["TransactItems"][0]["Update"]
            values = update["ExpressionAttributeValues"]
            if ":one" in values:
                table.active_count += int(values[":one"]["N"])
        return AWSResponse(
            request.url,
            200,
            {"content-type": "application/x-amz-json-1.0"},
            FakeResponseBody(),
        )

    resource.meta.client.meta.events.register("before-send.dynamodb.TransactWriteItems", capture)
    monkeypatch.setattr(jobs, "ddb", resource)
    return wire_requests


def test_post_jobs_sends_native_counter_as_number_on_actual_boto3_wire(monkeypatch):
    table = QuotaTable(19)
    wire_requests = install_wire_capture(monkeypatch, table)
    monkeypatch.setattr(jobs, "_table", lambda: table)
    monkeypatch.setattr(jobs.s3, "generate_presigned_url", lambda *_args, **_kwargs: "https://upload.example.test")
    monkeypatch.setattr(jobs, "MAX_USER_JOBS", 20)
    monkeypatch.setenv("DOCUMENTS_BUCKET", "documents")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "1000")
    event = {
        "httpMethod": "POST",
        "resource": "/jobs",
        "body": json.dumps({"filename": "report.pdf", "file_size": 10}),
        "requestContext": {"authorizer": {"claims": {"sub": "cognito-sub"}}},
    }

    response = jobs.lambda_handler(event, None)

    assert response["statusCode"] == 201
    assert json.loads(response["body"])["count"] == 20
    transaction = wire_requests[0]["TransactItems"]
    quota_update = transaction[0]["Update"]
    assert quota_update["Key"] == {"job_id": {"S": jobs._quota_key("cognito-sub")}}
    assert quota_update["ExpressionAttributeValues"][":one"] == {"N": "1"}
    assert quota_update["ExpressionAttributeValues"][":limit"] == {"N": "20"}
    assert ":one" not in quota_update["ExpressionAttributeValues"] or "M" not in quota_update["ExpressionAttributeValues"][":one"]
    assert transaction[1]["Put"]["Item"]["user_id"] == {"S": "cognito-sub"}
    assert transaction[1]["Put"]["Item"]["job_id"]["S"] == json.loads(response["body"])["job_id"]


def test_delete_transaction_sends_negative_number_with_nonnegative_guard(monkeypatch):
    wire_requests = install_wire_capture(monkeypatch)

    jobs._delete_job_transaction(QuotaTable(1), "job-to-delete", "cognito-sub")

    transaction = wire_requests[0]["TransactItems"]
    delete = transaction[0]["Delete"]
    quota_update = transaction[1]["Update"]
    assert delete["Key"] == {"job_id": {"S": "job-to-delete"}}
    assert quota_update["Key"] == {"job_id": {"S": jobs._quota_key("cognito-sub")}}
    assert quota_update["ExpressionAttributeValues"][":decrement"] == {"N": "-1"}
    assert quota_update["ExpressionAttributeValues"][":zero"] == {"N": "0"}
    assert quota_update["ConditionExpression"] == "#count > :zero"


def test_create_ask_transaction_checks_owner_status_and_rate_limit_on_wire(monkeypatch):
    resource = boto3.resource(
        "dynamodb",
        region_name="us-east-2",
        aws_access_key_id="unit-test-access-key",
        aws_secret_access_key="unit-test-secret-key",
    )
    requests = []

    def capture(request, **_kwargs):
        requests.append(json.loads(request.body))
        return AWSResponse(
            request.url,
            200,
            {"content-type": "application/x-amz-json-1.0"},
            FakeResponseBody(),
        )

    resource.meta.client.meta.events.register("before-send.dynamodb.TransactWriteItems", capture)
    monkeypatch.setattr(jobs, "ddb", resource)
    monkeypatch.setattr(jobs, "_table", lambda: type("Table", (), {"name": "jobs-table"})())
    monkeypatch.setattr(jobs, "_question_table", lambda: type("Table", (), {"name": "question-table"})())

    jobs._transact_create_ask(
        "job-1",
        "cognito-sub",
        {"job_id": "job-1", "ask_id": "ask-1", "user_id": "cognito-sub", "question": "¿Fecha?", "expires_at": 1},
        {"job_id": "__user_question_rate__#cognito-sub#2026-10-21", "ask_id": "__counter__"},
    )

    operations = requests[0]["TransactItems"]
    ownership_check = operations[0]["ConditionCheck"]
    assert ownership_check["TableName"] == "jobs-table"
    assert ownership_check["Key"] == {"job_id": {"S": "job-1"}}
    assert ownership_check["ConditionExpression"] == "user_id = :owner AND #status = :completed"

    rate_update = operations[1]["Update"]
    assert rate_update["TableName"] == "question-table"
    assert rate_update["ExpressionAttributeValues"][":one"] == {"N": "1"}
    assert rate_update["ExpressionAttributeValues"][":limit"] == {"N": "50"}
    assert "attribute_not_exists(#count)" in rate_update["ConditionExpression"]

    ask_put = operations[2]["Put"]
    assert ask_put["Item"]["question"] == {"S": "¿Fecha?"}
    assert ask_put["Item"]["user_id"] == {"S": "cognito-sub"}
    assert ask_put["Item"]["expires_at"] == {"N": "1"}
