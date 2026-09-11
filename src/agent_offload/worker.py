from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from agent_execution.identity import source_sha256 as execution_source_sha256
from agent_execution.worker import (
    WORKER_PROTOCOL_VERSION as EXECUTION_WORKER_PROTOCOL_VERSION,
)

from .protocol import (
    RESULT_SCHEMA,
    SUPPORTED_MODE,
    SUPPORTED_PROVIDER,
    SUPPORTED_SELECTOR,
    WORKER_PROTOCOL_VERSION,
    ProtocolError,
    sha256_text,
    validate_execution_result,
    worker_identity,
)

SOURCE_EXCLUDES = {
    ".git",
    ".jj",
    ".venv",
    "output",
    "outputs",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}


class WorkerError(RuntimeError):
    """The worker could not produce a trustworthy report."""


def utc_timestamp() -> float:
    return time.time()


def artifact_paths(task_id: str) -> tuple[Path, Path]:
    if not task_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for character in task_id
    ):
        raise WorkerError("task_id may contain only letters, digits, hyphens, and underscores")
    output_dir = Path.cwd() / "outputs"
    return (
        output_dir / f"agent-offload-result-{task_id}.json",
        output_dir / f"agent-offload-report-{task_id}.md",
    )


def read_prompt_payload(name: str, expected_sha256: str) -> str:
    if not name or Path(name).name != name:
        raise WorkerError("prompt payload must be a basename")
    payload_dir = os.environ.get("WEFT_PAYLOAD_DIR")
    if not payload_dir:
        raise WorkerError("WEFT_PAYLOAD_DIR is not set")
    path = Path(payload_dir) / name
    try:
        prompt = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkerError(f"cannot read prompt payload {name!r}: {exc}") from exc
    if not prompt.strip():
        raise WorkerError("prompt payload is empty")
    actual_sha256 = sha256_text(prompt)
    if actual_sha256 != expected_sha256:
        raise WorkerError(
            f"prompt payload digest mismatch: expected {expected_sha256}, got {actual_sha256}"
        )
    return prompt


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def extract_codex_report(stdout: str) -> str:
    reports: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            reports.append(text.strip())
    if not reports:
        raise WorkerError("Codex completed without a final agent report")
    return reports[-1]


def source_tree_sha256(root: Path) -> str:
    git = shutil.which("git")
    relative_paths: list[Path]
    if git is not None:
        listing = subprocess.run(
            [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            check=False,
        )
    else:
        listing = None
    if listing is not None and listing.returncode == 0:
        relative_paths = [
            Path(os.fsdecode(item))
            for item in listing.stdout.split(b"\0")
            if item and Path(os.fsdecode(item)).parts[0] not in SOURCE_EXCLUDES
        ]
    else:
        relative_paths = []
        for directory, names, files in os.walk(root):
            names[:] = [name for name in names if name not in SOURCE_EXCLUDES]
            base = Path(directory)
            relative_paths.extend((base / name).relative_to(root) for name in files)
    digest = hashlib.sha256()
    for relative in sorted(relative_paths, key=lambda item: item.as_posix()):
        if relative.is_absolute() or ".." in relative.parts:
            raise WorkerError(f"source inventory returned unsafe path: {relative}")
        path = root / relative
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise WorkerError(f"cannot stat source path {relative}: {exc}") from exc
        encoded = relative.as_posix().encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
        if path.is_symlink():
            target = os.readlink(path).encode()
            digest.update(b"L")
            digest.update(len(target).to_bytes(8, "big"))
            digest.update(target)
            continue
        if not path.is_file():
            continue
        digest.update(b"F")
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise WorkerError(f"cannot read source path {relative}: {exc}") from exc
    return digest.hexdigest()


def validate_harness_result(
    value: object,
    *,
    task_id: str,
    prompt_sha256: str,
    source_sha256: str,
) -> dict[str, Any]:
    try:
        return validate_execution_result(
            value,
            task_id=task_id,
            prompt_sha256=prompt_sha256,
            source_sha256=source_sha256,
        )
    except ProtocolError as exc:
        raise WorkerError(str(exc)) from exc


def resolve_codex() -> str | None:
    configured = os.environ.get("AGENT_OFFLOAD_CODEX")
    candidates = [configured] if configured else ["/opt/homebrew/bin/codex"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("codex")


def nested_command(
    *,
    task_id: str,
    selector: str,
    effort: str,
    prompt_payload: str,
    prompt_sha256: str,
    harness_result_path: Path,
    harness_timeout: float,
    ctx_timeout: float,
    worker_source_sha256: str,
) -> list[str]:
    execution_worker = shutil.which("agent-execution-worker")
    codex = resolve_codex()
    missing = [
        name
        for name, value in (
            ("agent-execution-worker", execution_worker),
            ("codex", codex),
        )
        if value is None
    ]
    if missing:
        raise WorkerError(f"required executable not found: {', '.join(missing)}")
    assert execution_worker is not None
    assert codex is not None
    return [
        execution_worker,
        "execute",
        "--provider",
        SUPPORTED_PROVIDER,
        "--model-call-id",
        task_id,
        "--harness-model",
        selector,
        "--expect-protocol",
        str(EXECUTION_WORKER_PROTOCOL_VERSION),
        "--expect-source-sha256",
        worker_source_sha256,
        "--prompt-payload",
        prompt_payload,
        "--expect-prompt-sha256",
        prompt_sha256,
        "--evidence-out",
        str(harness_result_path),
        "--timeout",
        str(harness_timeout),
        "--ctx-timeout",
        str(ctx_timeout),
        "--",
        codex,
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--cd",
        ".",
        "--skip-git-repo-check",
        "--config",
        f'model_reasoning_effort="{effort}"',
        "-",
    ]


def failure_report(message: str) -> str:
    detail = message.strip() or "The worker ended without a diagnostic."
    return f"# Agent offload failed\n\n{detail}\n"


def execute(
    *,
    task_id: str,
    provider: str,
    mode: str,
    selector: str,
    effort: str,
    prompt_payload: str,
    prompt_sha256: str,
    expect_protocol: int,
    expect_source_sha256: str,
    harness_timeout: float,
    ctx_timeout: float,
    runner: Any = subprocess.run,
) -> tuple[dict[str, Any], Path, Path]:
    started_at = utc_timestamp()
    result_path, report_path = artifact_paths(task_id)
    identity = worker_identity()
    result: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "task_id": task_id,
        "provider": provider,
        "mode": mode,
        "selector": selector,
        "effort": effort,
        "status": "preflight_failed",
        "prompt_sha256": prompt_sha256,
        "worker_identity": identity,
        "harness_result": None,
        "harness_session_id": None,
        "report": None,
        "report_sha256": None,
        "source_sha256_before": None,
        "source_sha256_after": None,
        "report_path": report_path.relative_to(Path.cwd()).as_posix(),
        "failure": "",
        "started_at": started_at,
        "completed_at": None,
        "worker_cwd": str(Path.cwd()),
    }
    try:
        if provider != SUPPORTED_PROVIDER:
            raise WorkerError(f"unsupported provider: {provider!r}")
        if mode != SUPPORTED_MODE:
            raise WorkerError(f"unsupported mode: {mode!r}")
        if selector != SUPPORTED_SELECTOR:
            raise WorkerError(f"unsupported selector: {selector!r}")
        if expect_protocol != WORKER_PROTOCOL_VERSION:
            raise WorkerError(
                f"worker protocol mismatch: expected {expect_protocol}, installed {WORKER_PROTOCOL_VERSION}"
            )
        if identity["source_sha256"] != expect_source_sha256:
            raise WorkerError(
                "worker source mismatch: "
                f"expected {expect_source_sha256}, installed {identity['source_sha256']}"
            )
        read_prompt_payload(prompt_payload, prompt_sha256)
        nested_worker_source_sha256 = execution_source_sha256()
        result["source_sha256_before"] = source_tree_sha256(Path.cwd())
        result["status"] = "harness_failed"
        harness_result_path = Path.cwd() / "outputs" / f".agent-offload-harness-{task_id}.json"
        command = nested_command(
            task_id=task_id,
            selector=selector,
            effort=effort,
            prompt_payload=prompt_payload,
            prompt_sha256=prompt_sha256,
            harness_result_path=harness_result_path,
            harness_timeout=harness_timeout,
            ctx_timeout=ctx_timeout,
            worker_source_sha256=nested_worker_source_sha256,
        )
        timeout = harness_timeout + ctx_timeout + 60
        completed = runner(
            command,
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if not harness_result_path.is_file():
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise WorkerError(
                f"agent-execution worker exited {completed.returncode} without evidence: {detail}"
            )
        try:
            harness_value = json.loads(harness_result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerError(f"cannot read agent-execution worker evidence: {exc}") from exc
        harness_result = validate_harness_result(
            harness_value,
            task_id=task_id,
            prompt_sha256=prompt_sha256,
            source_sha256=nested_worker_source_sha256,
        )
        result["harness_result"] = harness_result
        result["harness_session_id"] = harness_result.get("session_id")
        harness_status = harness_result.get("status")
        if harness_status != "completed":
            result["status"] = "harness_failed"
            result["failure"] = str(harness_result.get("failure") or "agent harness failed")
        else:
            harness = harness_result["harness"]
            report = extract_codex_report(str(harness.get("stdout") or ""))
            result["status"] = "completed"
            result["report"] = report
            result["report_sha256"] = sha256_text(report)
    except (WorkerError, subprocess.TimeoutExpired, OSError) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            result["status"] = "harness_failed"
            result["failure"] = f"agent-execution worker exceeded {exc.timeout} seconds"
        elif isinstance(exc, OSError):
            result["status"] = "harness_failed"
            result["failure"] = f"cannot launch agent-execution worker: {exc}"
        else:
            result["failure"] = str(exc)
    finally:
        before = result.get("source_sha256_before")
        if isinstance(before, str):
            try:
                after = source_tree_sha256(Path.cwd())
                result["source_sha256_after"] = after
                if after != before:
                    result["status"] = "evidence_failed"
                    result["failure"] = "Codex modified the disposable source snapshot"
                    result["report"] = None
                    result["report_sha256"] = None
            except WorkerError as exc:
                result["status"] = "evidence_failed"
                result["failure"] = f"cannot verify the source snapshot after execution: {exc}"
                result["report"] = None
                result["report_sha256"] = None
        result["completed_at"] = utc_timestamp()
        if result["status"] == "completed":
            atomic_write(report_path, str(result["report"]) + "\n")
        else:
            atomic_write(report_path, failure_report(str(result["failure"])))
        atomic_write(result_path, json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return result, result_path, report_path
