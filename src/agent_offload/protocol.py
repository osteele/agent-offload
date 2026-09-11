from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_execution.identity import source_sha256 as execution_source_sha256
from agent_execution.worker import (
    WORKER_PROTOCOL_VERSION as EXECUTION_WORKER_PROTOCOL_VERSION,
)
from agent_execution.worker import (
    WorkerResult,
)

from . import __version__

WORKER_PROTOCOL_VERSION = 1
IDENTITY_SCHEMA = "agent-offload.worker-identity/v1"
RESULT_SCHEMA = "agent-offload.worker-result/v1"
DOCTOR_SCHEMA = "agent-offload.doctor/v1"
RETRIEVAL_SCHEMA = "agent-offload.retrieval/v1"
SUPPORTED_PROVIDER = "codex"
SUPPORTED_MODE = "report"
SUPPORTED_SELECTOR = "gpt-5.5"
NATIVE_DISPATCH_RETIRED = (
    "native Codex execution is retired; use the grounded OMP Anthropic route "
    "or an OpenAI packet-only route"
)
RESULT_STATUSES = {"completed", "preflight_failed", "harness_failed", "evidence_failed"}


class ProtocolError(ValueError):
    """A remote artifact does not satisfy the agent-offload protocol."""


def package_source_sha256() -> str:
    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(
        package_root.rglob("*.py"), key=lambda item: item.relative_to(package_root).as_posix()
    ):
        relative = path.relative_to(package_root).as_posix().encode()
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def worker_identity() -> dict[str, Any]:
    return {
        "schema_version": IDENTITY_SCHEMA,
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "package_version": __version__,
        "source_sha256": package_source_sha256(),
        "package_root": str(Path(__file__).resolve().parent),
    }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("worker identity must be a JSON object")
    if value.get("schema_version") != IDENTITY_SCHEMA:
        raise ProtocolError(f"unexpected worker identity schema: {value.get('schema_version')!r}")
    if value.get("protocol_version") != WORKER_PROTOCOL_VERSION:
        raise ProtocolError(f"unexpected worker protocol: {value.get('protocol_version')!r}")
    source_sha256 = value.get("source_sha256")
    if not isinstance(source_sha256, str) or len(source_sha256) != 64:
        raise ProtocolError("worker identity has an invalid source_sha256")
    return value


def validate_execution_result(
    value: object,
    *,
    task_id: str,
    prompt_sha256: str,
    source_sha256: str,
) -> dict[str, Any]:
    try:
        execution_result = WorkerResult.parse(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"invalid agent-execution worker result: {exc}") from exc
    if execution_result.worker_protocol_version != EXECUTION_WORKER_PROTOCOL_VERSION:
        raise ProtocolError("nested execution worker protocol does not match the conductor")
    if execution_result.worker_source_sha256 != source_sha256:
        raise ProtocolError("nested execution worker source does not match the conductor")
    if execution_result.model_call_id != task_id:
        raise ProtocolError("nested execution task identifier does not match")
    if execution_result.provider != SUPPORTED_PROVIDER:
        raise ProtocolError("nested execution provider does not match")
    if execution_result.prompt_sha256 != prompt_sha256:
        raise ProtocolError("nested execution prompt digest does not match")
    return execution_result.to_dict()


def validate_result(
    value: object,
    *,
    task_id: str | None = None,
    prompt_sha256: str | None = None,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("worker result must be a JSON object")
    if value.get("schema_version") != RESULT_SCHEMA:
        raise ProtocolError(f"unexpected result schema: {value.get('schema_version')!r}")
    if value.get("provider") != SUPPORTED_PROVIDER or value.get("mode") != SUPPORTED_MODE:
        raise ProtocolError("worker result is not a Codex report result")
    if value.get("selector") != SUPPORTED_SELECTOR:
        raise ProtocolError(f"unexpected Codex selector: {value.get('selector')!r}")
    status = value.get("status")
    if status not in RESULT_STATUSES:
        raise ProtocolError(f"unexpected result status: {status!r}")
    if task_id is not None and value.get("task_id") != task_id:
        raise ProtocolError("worker result task_id does not match the submitted task")
    if prompt_sha256 is not None and value.get("prompt_sha256") != prompt_sha256:
        raise ProtocolError("worker result prompt digest does not match the submitted brief")
    identity = validate_identity(value.get("worker_identity"))
    if source_sha256 is not None and identity.get("source_sha256") != source_sha256:
        raise ProtocolError("remote worker source does not match the submitting conductor")
    harness = value.get("harness_result")
    execution_result = (
        validate_execution_result(
            harness,
            task_id=str(value.get("task_id") or ""),
            prompt_sha256=str(value.get("prompt_sha256") or ""),
            source_sha256=execution_source_sha256(),
        )
        if harness is not None
        else None
    )
    report = value.get("report")
    if status == "completed":
        if not isinstance(report, str) or not report.strip():
            raise ProtocolError("completed worker result has no report")
        if value.get("report_sha256") != sha256_text(report):
            raise ProtocolError("completed worker result has an invalid report digest")
        if execution_result is None or execution_result["status"] != "completed":
            raise ProtocolError("completed worker result has no completed harness result")
        source_before = value.get("source_sha256_before")
        if (
            not isinstance(source_before, str)
            or len(source_before) != 64
            or value.get("source_sha256_after") != source_before
        ):
            raise ProtocolError("completed worker result did not preserve its source snapshot")
    return value


def load_json_object(text: str, *, subject: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"{subject} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError(f"{subject} must be a JSON object")
    return value
