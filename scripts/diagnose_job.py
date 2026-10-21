#!/usr/bin/env python3
"""One-shot, read-only AWS trace for one document job.

The script uses CloudFormation to discover physical resource names, reads one
DynamoDB item and queue/service metadata, and fetches bounded CloudWatch events.
It never receives an SQS message because ReceiveMessage changes message
visibility/receive count and is therefore not read-only in operational terms.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


JOB_ID_RE = re.compile(r"^[0-9a-fA-F-]{16,64}$")
MAX_WINDOW_MINUTES = 24 * 60
MAX_LOG_EVENTS = 500


def utc_iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def run_aws(args: list[str], *, profile: str | None, region: str) -> tuple[dict, str | None]:
    command = ["aws", "--no-cli-pager", "--region", region]
    if profile:
        command += ["--profile", profile]
    command += args + ["--output", "json"]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        return {}, completed.stderr.strip() or f"AWS CLI exit {completed.returncode}"
    try:
        return json.loads(completed.stdout or "{}"), None
    except json.JSONDecodeError:
        return {}, "AWS CLI returned invalid JSON"


def aws_section(lines: list[str], title: str, args: list[str], *, profile: str | None, region: str) -> dict:
    payload, error = run_aws(args, profile=profile, region=region)
    lines.append(f"## {title}\n")
    if error:
        lines.append(f"No disponible: {error}\n")
    else:
        lines.append("```json\n" + json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n```\n")
    return payload


def physical_id(resources: dict, logical_id: str) -> str | None:
    for item in resources.get("StackResources", []):
        if item.get("LogicalResourceId") == logical_id:
            return item.get("PhysicalResourceId")
    return None


def output_value(stack: dict, key: str) -> str | None:
    for item in (stack.get("Stacks") or [{}])[0].get("Outputs", []):
        if item.get("OutputKey") == key:
            return item.get("OutputValue")
    return None


def safe_job_item(item: dict) -> dict:
    fields = (
        "job_id", "status", "created_at", "updated_at", "started_at", "completed_at", "failed_at",
        "error", "last_error", "current_page", "total_pages", "progress_percent", "processing_attempt",
        "processing_lease_until", "processing_message_id", "result_key",
    )
    result = {key: item[key] for key in fields if key in item}
    result["has_source_object_key"] = bool(item.get("object_key"))
    result["has_result_key"] = bool(item.get("result_key"))
    result.pop("result_key", None)
    # Never include user_prompt, user_id or original filename in the report.
    return result


def safe_ecs_task(task: dict) -> dict:
    fields = (
        "taskArn", "taskDefinitionArn", "lastStatus", "desiredStatus", "stopCode", "stoppedReason",
        "createdAt", "startedAt", "stoppedAt", "healthStatus", "launchType",
    )
    result = {key: task[key] for key in fields if key in task}
    result["containers"] = [
        {key: container[key] for key in ("name", "lastStatus", "exitCode", "reason", "healthStatus") if key in container}
        for container in task.get("containers", [])
    ]
    return result


def parse_event_message(message: str) -> dict | None:
    start = message.find("{")
    if start >= 0:
        message = message[start:]
    try:
        value = json.loads(message)
        return value if isinstance(value, dict) else None
    except (TypeError, json.JSONDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera una traza AWS de solo lectura para un job.")
    parser.add_argument("job_id", help="UUID del job visible en el frontend/DynamoDB")
    parser.add_argument("--stack", default="document-ai-pipeline")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--window-minutes", type=int, default=120)
    parser.add_argument("--max-log-events", type=int, default=MAX_LOG_EVENTS)
    parser.add_argument("--profile", default=os.getenv("AWS_PROFILE"))
    args = parser.parse_args()

    if not JOB_ID_RE.fullmatch(args.job_id):
        parser.error("job_id debe ser un UUID/identificador hexadecimal válido")
    if not 5 <= args.window_minutes <= MAX_WINDOW_MINUTES:
        parser.error("--window-minutes debe estar entre 5 y 1440")
    if not 1 <= args.max_log_events <= MAX_LOG_EVENTS:
        parser.error(f"--max-log-events debe estar entre 1 y {MAX_LOG_EVENTS}")

    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(minutes=args.window_minutes)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    lines = [
        f"# Diagnóstico de job {args.job_id}\n",
        f"Stack: `{args.stack}`; región: `{args.region}`\n",
        f"Ventana UTC: `{utc_iso(start)}` a `{utc_iso(end)}` ({args.window_minutes} min).\n",
        "Este reporte puede contener texto sensible si `DEBUG_MODEL_OUTPUT=true` estuvo habilitado. "
        "El archivo se crea con permisos privados (0600).\n",
        "\n",
    ]

    stack, error = run_aws(
        ["cloudformation", "describe-stacks", "--stack-name", args.stack],
        profile=args.profile,
        region=args.region,
    )
    if error:
        print(f"No se pudo describir el stack: {error}", file=sys.stderr)
        return 2
    resources, error = run_aws(
        ["cloudformation", "describe-stack-resources", "--stack-name", args.stack],
        profile=args.profile,
        region=args.region,
    )
    if error:
        print(f"No se pudieron describir recursos del stack: {error}", file=sys.stderr)
        return 2

    table = output_value(stack, "JobsTableName")
    queue_url = output_value(stack, "DocumentQueueUrl")
    cluster = output_value(stack, "DocumentProcessorClusterName")
    service = output_value(stack, "DocumentProcessorServiceName")
    queue_arn = output_value(stack, "DocumentQueueArn")
    processor_log_group = physical_id(resources, "ProcessorLogGroup")
    processor_function = physical_id(resources, "DocumentProcessorFunction")
    jobs_api_function = physical_id(resources, "JobsApiFunction")
    dlq_url = physical_id(resources, "DocumentDeadLetterQueue")

    discovered = {
        "JobsTable": table,
        "DocumentQueueUrl": queue_url,
        "DocumentQueueArn": queue_arn,
        "DeadLetterQueueUrl": dlq_url,
        "ProcessorLogGroup": processor_log_group,
        "DocumentProcessorFunction": processor_function,
        "JobsApiFunction": jobs_api_function,
        "EcsCluster": cluster,
        "EcsService": service,
    }
    lines += ["## Recursos descubiertos desde CloudFormation\n", "```json\n", json.dumps(discovered, indent=2), "\n```\n"]

    if table:
        key = json.dumps({"job_id": {"S": args.job_id}})
        payload, error = run_aws(
            ["dynamodb", "get-item", "--table-name", table, "--key", key, "--consistent-read"],
            profile=args.profile,
            region=args.region,
        )
        lines.append("## Estado DynamoDB\n")
        if error:
            lines.append(f"No disponible: {error}\n")
        else:
            item = payload.get("Item", {})
            decoded = {key: next(iter(value.values())) for key, value in item.items() if isinstance(value, dict) and len(value) == 1}
            lines.append("```json\n" + json.dumps(safe_job_item(decoded), ensure_ascii=False, indent=2, default=str) + "\n```\n")
    else:
        lines.append("## Estado DynamoDB\nJobsTableName no aparece en Outputs.\n")

    for label, url in (("DocumentQueue", queue_url), ("DeadLetterQueue", dlq_url)):
        if not url:
            continue
        aws_section(
            lines,
            f"Atributos SQS: {label}",
            [
                "sqs", "get-queue-attributes", "--queue-url", url, "--attribute-names",
                "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed", "VisibilityTimeout", "RedrivePolicy",
            ],
            profile=args.profile,
            region=args.region,
        )

    timeline: list[dict] = []
    log_query_errors: list[str] = []
    for logical_id, function_name in (
        ("DocumentProcessorFunction", processor_function),
        ("JobsApiFunction", jobs_api_function),
    ):
        if not function_name:
            continue
        group = f"/aws/lambda/{function_name}"
        logs, error = run_aws(
            [
                "logs", "filter-log-events", "--log-group-name", group,
                "--start-time", str(start_ms), "--end-time", str(end_ms),
                "--filter-pattern", f'"{args.job_id}"', "--limit", str(args.max_log_events), "--no-paginate",
            ],
            profile=args.profile,
            region=args.region,
        )
        if error:
            log_query_errors.append(f"{group}: {error}")
        else:
            for event in logs.get("events", []):
                timeline.append({
                    "timestamp": event.get("timestamp", 0),
                    "source": f"Lambda/{logical_id}",
                    "stream": event.get("logStreamName", ""),
                    "message": event.get("message", ""),
                })

    if processor_log_group:
        logs, error = run_aws(
            [
                "logs", "filter-log-events", "--log-group-name", processor_log_group,
                "--start-time", str(start_ms), "--end-time", str(end_ms),
                "--filter-pattern", f'"{args.job_id}"', "--limit", str(args.max_log_events), "--no-paginate",
            ],
            profile=args.profile,
            region=args.region,
        )
        if error:
            log_query_errors.append(f"{processor_log_group}: {error}")
        else:
            for event in logs.get("events", []):
                timeline.append({
                    "timestamp": event.get("timestamp", 0),
                    "source": "Fargate/document-processor",
                    "stream": event.get("logStreamName", ""),
                    "message": event.get("message", ""),
                })

    if cluster and service:
        service_data, error = run_aws(
            ["ecs", "describe-services", "--cluster", cluster, "--services", service],
            profile=args.profile,
            region=args.region,
        )
        lines.append("## ECS service / events\n")
        if error:
            lines.append(f"No disponible: {error}\n")
        else:
            current = (service_data.get("services") or [{}])[0]
            safe_service = {
                key: current.get(key)
                for key in ("serviceName", "status", "desiredCount", "runningCount", "pendingCount", "taskDefinition", "deployments")
                if key in current
            }
            event_rows = []
            for event in current.get("events", []):
                created = event.get("createdAt")
                try:
                    created_dt = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
                except (AttributeError, ValueError):
                    continue
                if created_dt >= start:
                    event_rows.append({"createdAt": created, "message": event.get("message")})
                    timeline.append({
                        "timestamp": int(created_dt.timestamp() * 1000),
                        "source": "ECS/service-event",
                        "stream": service,
                        "message": event.get("message", ""),
                    })
            lines.append("```json\n" + json.dumps({"service": safe_service, "recent_events": event_rows}, ensure_ascii=False, indent=2, default=str) + "\n```\n")

        stopped, error = run_aws(
            ["ecs", "list-tasks", "--cluster", cluster, "--service-name", service, "--desired-status", "STOPPED", "--max-results", "20"],
            profile=args.profile,
            region=args.region,
        )
        running, run_error = run_aws(
            ["ecs", "list-tasks", "--cluster", cluster, "--service-name", service, "--desired-status", "RUNNING", "--max-results", "20"],
            profile=args.profile,
            region=args.region,
        )
        task_arns = (stopped.get("taskArns", []) if not error else []) + (running.get("taskArns", []) if not run_error else [])
        lines.append("## ECS tasks\n")
        if error or run_error:
            lines.append(f"Stopped tasks: {error or 'ok'}; running tasks: {run_error or 'ok'}\n")
        elif task_arns:
            tasks, error = run_aws(
                ["ecs", "describe-tasks", "--cluster", cluster, "--tasks", *task_arns[:40]],
                profile=args.profile,
                region=args.region,
            )
            if error:
                lines.append(f"No disponible: {error}\n")
            else:
                task_rows = []
                for task in tasks.get("tasks", []):
                    row = safe_ecs_task(task)
                    stopped_at = row.get("stoppedAt")
                    if row.get("lastStatus") == "STOPPED" and stopped_at:
                        try:
                            when = dt.datetime.fromisoformat(stopped_at.replace("Z", "+00:00"))
                            if when < start:
                                continue
                        except ValueError:
                            pass
                    task_rows.append(row)
                lines.append("```json\n" + json.dumps(task_rows, ensure_ascii=False, indent=2, default=str) + "\n```\n")
        else:
            lines.append("No hay tasks visibles en las listas recientes.\n")

        metric_rows = {}
        for metric in ("CPUUtilization", "MemoryUtilization"):
            values, error = run_aws(
                [
                    "cloudwatch", "get-metric-statistics", "--namespace", "AWS/ECS", "--metric-name", metric,
                    "--dimensions", f"Name=ClusterName,Value={cluster}", f"Name=ServiceName,Value={service}",
                    "--statistics", "Average", "Maximum", "--period", "300",
                    "--start-time", utc_iso(start), "--end-time", utc_iso(end),
                ],
                profile=args.profile,
                region=args.region,
            )
            metric_rows[metric] = {"datapoints": values.get("Datapoints", [])} if not error else {"error": error}
        lines.append("## Métricas ECS (ventana agregada de 5 minutos)\n```json\n")
        lines.append(json.dumps(metric_rows, ensure_ascii=False, indent=2, default=str) + "\n```\n")

    lines.append("## Línea de tiempo cronológica (CloudWatch y eventos ECS)\n")
    for error in log_query_errors:
        lines.append(f"No disponible: {error}\n")
    for event in sorted(timeline, key=lambda item: item["timestamp"]):
        when = dt.datetime.fromtimestamp(event["timestamp"] / 1000, tz=dt.timezone.utc).isoformat()
        parsed = parse_event_message(event["message"])
        body = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")) if parsed else event["message"]
        lines.append(f"- `{when}` `{event['source']}` `{event['stream']}` {body}\n")
    if not timeline:
        lines.append("Sin eventos coincidentes en la ventana consultada.\n")

    lines.extend([
        "## Interpretación de límites\n",
        "- `ApproximateNumberOfMessages*` son métricas agregadas; no identifican un job individual.\n",
        "- El script no llama `ReceiveMessage`: hacerlo incrementaría/afectaría la visibilidad del mensaje. "
        "`ApproximateReceiveCount` por job queda registrado en `JOB_RECEIVED` para nuevas ejecuciones.\n",
        "- Los eventos CloudWatch se limitan a una ventana y una página de hasta el máximo indicado; "
        "si faltan eventos, vuelva a ejecutar con una ventana menor/ajustada, no con polling.\n",
    ])

    report_dir = Path(tempfile.mkdtemp(prefix=f"aws-ai-docs-{args.job_id[:8]}-"))
    os.chmod(report_dir, stat.S_IRWXU)
    report_path = report_dir / "diagnosis.md"
    report_path.write_text("".join(lines), encoding="utf-8")
    os.chmod(report_path, stat.S_IRUSR | stat.S_IWUSR)
    print(f"Reporte de solo lectura guardado en: {report_path}")
    print("Revisa el archivo localmente; si DEBUG_MODEL_OUTPUT estuvo activo, puede contener texto sensible de la IA.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
