"""Observe the target worker's pinned OMP catalog and credentials without generation."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from agent_execution.omp_execution import OMP_WRITER_SELECTORS

from .task_protocol import TaskError, object_json

PROBE_SCHEMA = "agent-execution.writer-readiness/v1"
IDENTITY_SCHEMA = "agent-execution.worker-identity/v1"
PROBE_TIMEOUT = 30


def probe_writers(
    *,
    host: str = "studio",
    account: str = "agent",
    selectors: list[str] | None = None,
    bin_dir: str | None = None,
    execution_sha256: str | None = None,
) -> list[dict]:
    requested = sorted(OMP_WRITER_SELECTORS if selectors is None else set(selectors))
    if not requested or any(selector not in OMP_WRITER_SELECTORS for selector in requested):
        raise TaskError("readiness requires admitted exact writer selectors")
    target = host if "@" in host else f"{account}@{host}"
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9_][A-Za-z0-9_.:-]*", target):
        raise TaskError(f"invalid readiness SSH target: {target!r}")
    executable = "agent-offload-worker"
    if bin_dir is not None:
        if not Path(bin_dir).is_absolute():
            raise TaskError("readiness worker bin_dir must be absolute")
        executable = str(Path(bin_dir) / executable)
    command = [executable, "probe-writers", "--timeout", str(PROBE_TIMEOUT)]
    for selector in requested:
        command.extend(["--selector", selector])
    remote = (
        'export PATH="$HOME/.local/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; '
        + shlex.join(command)
    )
    try:
        completed = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, remote],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT + 15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TaskError(f"writer readiness probe on {target} failed: {error}") from error
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or str(completed.returncode)
        raise TaskError(f"writer readiness probe on {target} failed: {detail}")
    value = object_json(completed.stdout, PROBE_SCHEMA)
    identity = value.get("worker_identity")
    if (
        not isinstance(identity, dict)
        or identity.get("schema_version") != IDENTITY_SCHEMA
        or not isinstance(identity.get("source_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", identity["source_sha256"])
    ):
        raise TaskError("writer readiness probe returned malformed worker identity")
    if execution_sha256 is not None and identity["source_sha256"] != execution_sha256:
        raise TaskError("writer readiness probe execution identity differs from the task dispatch")
    writers = value.get("writers")
    if not isinstance(writers, list):
        raise TaskError("writer readiness probe lacks writer rows")
    observed = set()
    for row in writers:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("selector"), str)
            or row["selector"] not in requested
            or row["selector"] in observed
            or any(
                type(row.get(key)) is not bool
                for key in ("model_available", "credential_available", "available")
            )
            or row["available"] != (row["model_available"] and row["credential_available"])
            or not isinstance(row.get("detail"), str)
            or not row["detail"].strip()
        ):
            raise TaskError("writer readiness probe returned malformed or inconsistent writer rows")
        observed.add(row["selector"])
    if observed != set(requested):
        raise TaskError("writer readiness probe omitted requested selectors")
    return writers


def require_writer(record: dict) -> None:
    """Refuse queueing unless the exact provisioned writer is currently ready."""
    selector = record["request"]["selector"]
    row = probe_writers(
        host=record["host"],
        account=record.get("account", "agent"),
        selectors=[selector],
        bin_dir=record["bin_dir"],
        execution_sha256=record["request"]["execution_sha256"],
    )[0]
    if not row["available"]:
        raise TaskError(f"writer {selector} is unavailable on {record['host']}: {row['detail']}")
