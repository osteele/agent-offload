"""Execute a coding task in Weft's disposable snapshot and publish its changes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from agent_execution.identity import source_sha256
from agent_execution.omp_execution import omp_final_text
from agent_execution.worker import WORKER_PROTOCOL_VERSION, execute_worker, run_worker_command

from .protocol import package_source_sha256
from .task_protocol import (
    RESULT_SCHEMA,
    SCHEMA,
    TaskError,
    digest,
    encode,
    file_bytes,
    manifest,
    object_json,
    result_path,
    safe_path,
    validate_changes,
    walk_files,
)


def _validation_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value[-100000:].decode("utf-8", errors="replace")
    return (value or "")[-100000:]


def execute(request: dict, root: Path) -> dict:
    baseline = request["baseline"]
    if manifest(root, list(baseline)) != baseline:
        raise TaskError("remote snapshot differs from the submitted baseline")
    # Record all materialized ordinary files, including files outside the edit
    # manifest, so changes to unexpected files cannot masquerade as additions.
    before = {name: file_bytes(safe_path(root, name)) for name in walk_files(root)}
    worker = execute_worker(
        provider="omp",
        model_call_id=request["id"],
        command=[
            "omp",
            "-p",
            request["brief"],
            "--mode",
            "json",
            "--cwd",
            ".",
            "--execution-tool-policy",
            "workspace-write-no-shell",
        ],
        harness_model=request["selector"],
        timeout=request["timeout"],
        ctx_timeout=30,
        expect_protocol=WORKER_PROTOCOL_VERSION,
        expect_source_sha256=request["execution_sha256"],
        cwd=root,
    )
    after_names = set(walk_files(root))
    changes = []
    for name in sorted(set(before) | after_names):
        path = safe_path(root, name)
        old = before.get(name)
        new = file_bytes(path) if name in after_names else None
        old_mode = baseline.get(name, {}).get("mode")
        new_mode = path.stat().st_mode & 0o777 if new is not None else None
        if old == new and (name not in baseline or old_mode == new_mode):
            continue
        if name in before and name not in baseline:
            raise TaskError(f"worker changed a file outside the submitted edit manifest: {name}")
        changes.append(
            {
                "path": name,
                "before": encode(old) if old is not None else None,
                "after": encode(new) if new is not None else None,
                "before_mode": old_mode,
                "after_mode": new_mode,
            }
        )
    validate_changes(request, changes)
    status = "completed" if worker.status == "completed" else "failed"
    report = (
        omp_final_text(worker.harness.stdout) if worker.status == "completed" else worker.failure
    )
    validation = None
    if status == "completed" and request["check"]:
        proposed_state = manifest(root, walk_files(root))
        timeout = min(request["timeout"], 900)
        try:
            checked = run_worker_command(request["check"], root, timeout)
        except subprocess.TimeoutExpired as error:
            validation = {
                "command": request["check"],
                "exit_code": None,
                "stdout": _validation_output(error.stdout),
                "stderr": _validation_output(error.stderr),
                "error": f"validation timed out after {timeout:g} seconds",
            }
        else:
            validation = {
                "command": request["check"],
                "exit_code": checked.exit_status,
                "stdout": _validation_output(checked.stdout),
                "stderr": _validation_output(checked.stderr),
            }
        if validation["exit_code"] != 0:
            status = "validation_failed"
        # Validation must test the exact source state represented by the
        # proposal. A census that cannot even be taken, such as one finding a
        # new oversized file, also means the check changed the tree.
        try:
            preserved = manifest(root, walk_files(root)) == proposed_state
        except TaskError:
            preserved = False
        if not preserved:
            status = "validation_failed"
            validation["error"] = "; ".join(
                filter(None, [validation.get("error"), "validation modified proposed source"])
            )
    return {
        "schema_version": RESULT_SCHEMA,
        "id": request["id"],
        "status": status,
        "offload_sha256": package_source_sha256(),
        "request_sha256": request["request_sha256"],
        "worker": worker.to_dict(),
        "report": report,
        "changes": changes,
        "validation": validation,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    try:
        payload = Path(os.environ["WEFT_PAYLOAD_DIR"]) / "coding-task"
        data = payload.read_bytes()
        if digest(data) != args.request_sha256:
            raise TaskError("task payload digest mismatch")
        request = object_json(data.decode(), SCHEMA)
        if request["id"] != args.task_id:
            raise TaskError("task identity mismatch")
        # Strict provenance pinning is the default, ratified 2026-09-26: a dispatch
        # is bound to the exact tooling bytes that produced it, and a mismatch is
        # refused rather than warned about. The rejected alternative -- warn-and-
        # continue -- would convert the provenance guarantee behind task
        # retrieve/apply into a best-effort label that every downstream consumer
        # inherits. The refusal is cheap: surfaced as failed_without_result before
        # real work, retryable via `task resume` after `agent-host-sync`.
        if request["offload_sha256"] != package_source_sha256():
            raise TaskError("installed agent-offload differs from the submitting version")
        if request["execution_sha256"] != source_sha256():
            raise TaskError("installed agent-execution differs from the submitting version")
        request["request_sha256"] = args.request_sha256
        try:
            result = execute(request, Path.cwd())
        except (OSError, ValueError, KeyError) as error:
            # Keep setup/census failures retrievable even when no harness ran.
            result = {
                "schema_version": RESULT_SCHEMA,
                "id": request["id"],
                "status": "failed",
                "offload_sha256": package_source_sha256(),
                "request_sha256": args.request_sha256,
                "worker": None,
                "report": f"task worker failed: {error}",
                "changes": [],
                "validation": None,
            }
        output = Path(result_path(args.task_id))
        parent = Path.cwd()
        for part in output.parent.parts:
            parent = parent / part
            if parent.is_symlink():
                raise TaskError("task result directory is a symlink")
            parent.mkdir(exist_ok=True)
        if output.is_symlink():
            raise TaskError("task result is a symlink")
        with output.open("x") as stream:
            json.dump(result, stream)
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({"id": args.task_id, "status": result["status"], "artifact": str(output)}))
        if result["status"] != "completed":
            raise SystemExit(1)
    except (OSError, ValueError, KeyError) as error:
        print(f"agent-offload task worker: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
