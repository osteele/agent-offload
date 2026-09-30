from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

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


def source_sha256(package_root: Path) -> str:
    """Hash package Python source, excluding hidden paths and installed node_modules.

    Relay dependency installs must not change the worker's source identity.
    """
    digest = hashlib.sha256()
    for path in sorted(
        (
            path
            for path in package_root.rglob("*.py")
            if all(
                part != "node_modules" and not part.startswith(".")
                for part in path.relative_to(package_root).parts
            )
        ),
        key=lambda item: item.relative_to(package_root).as_posix(),
    ):
        relative = path.relative_to(package_root).as_posix().encode()
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def package_source_sha256() -> str:
    return source_sha256(Path(__file__).resolve().parent)


#: Depth bound for the walk that locates an installed build's marker. The
#: installed layout is builds/<tool>/<id>/tools/<name>/lib/python3.x/
#: site-packages/agent_offload, so twelve levels reach the marker from any
#: module in the package while keeping the walk off unrelated ancestors.
_BUILD_MARKER_DEPTH = 12


def installed_build_id(start: Path | None = None) -> str | None:
    """Return the content-addressed build this package runs from, if any.

    A build directory carries a ``BUILD_ID`` file whose content is the
    directory's own name, so a stray marker elsewhere on the filesystem
    cannot be mistaken for one. A source checkout has no marker and reports
    ``None``: running uninstalled is a legitimate state, not a failure.
    """
    origin = (start or Path(__file__)).resolve()
    for parent in list(origin.parents)[:_BUILD_MARKER_DEPTH]:
        marker = parent / "BUILD_ID"
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == parent.name:
            return parent.name
    return None


def worker_identity() -> dict[str, Any]:
    """Describe this installation, including the identities it depends on.

    The relay digest and the execution worker's identity are reported by the
    installation itself rather than recomputed by a caller against the
    installed tree. A caller cannot reliably reach this package's interpreter
    from outside: an installed console script may be a shell wrapper rather
    than a direct-shebang script whenever its interpreter path is long.
    """
    from agent_execution.worker import installed_worker_identity

    from .relay import relay_source_sha256

    return {
        "schema_version": IDENTITY_SCHEMA,
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "package_version": __version__,
        "source_sha256": package_source_sha256(),
        "package_root": str(Path(__file__).resolve().parent),
        "build_id": installed_build_id(),
        "relay_source_sha256": relay_source_sha256(),
        "execution": installed_worker_identity().to_dict(),
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
    source_sha256: str | None,
    expected_protocol_version: int,
) -> dict[str, Any]:
    try:
        execution_result = WorkerResult.parse(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"invalid agent-execution worker result: {exc}") from exc
    if execution_result.worker_protocol_version != expected_protocol_version:
        raise ProtocolError("nested execution worker protocol does not match the dispatch")
    if execution_result.model_call_id != task_id:
        raise ProtocolError("nested execution task identifier does not match")
    if execution_result.provider != SUPPORTED_PROVIDER:
        raise ProtocolError("nested execution provider does not match")
    if execution_result.prompt_sha256 != prompt_sha256:
        raise ProtocolError("nested execution prompt digest does not match")
    if source_sha256 is None:
        raise ProtocolError(
            "historical nested execution evidence is unverifiable: "
            "the accepted offload dispatch did not retain a nested worker source baseline"
        )
    if execution_result.worker_source_sha256 != source_sha256:
        raise ProtocolError("nested execution worker source does not match the dispatch")
    return execution_result.to_dict()


def validate_result(
    value: object,
    *,
    task_id: str | None = None,
    prompt_sha256: str | None = None,
    source_sha256: str | None = None,
    execution_source_sha256: str | None = None,
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
            source_sha256=execution_source_sha256,
            # Native offload was retired while its nested worker used protocol 1.
            expected_protocol_version=1,
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
