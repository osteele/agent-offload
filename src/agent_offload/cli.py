from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import (
    DOCTOR_SCHEMA,
    RETRIEVAL_SCHEMA,
    SUPPORTED_MODE,
    SUPPORTED_PROVIDER,
    SUPPORTED_SELECTOR,
    WORKER_PROTOCOL_VERSION,
    ProtocolError,
    load_json_object,
    sha256_text,
    validate_result,
    worker_identity,
)
from .worker import failure_report

WEFT_RECEIPT_SCHEMA = "weft.run.receipt.v1"
TERMINAL_STATUSES = {"completed", "failed", "dead", "canceled", "cancelled", "killed"}
RESULT_PATTERN = re.compile(r"^outputs/agent-offload-result-([A-Za-z0-9_-]+)\.json$")
REPORT_PREAMBLE = """You are executing a report-only repository task.

- Work read-only. Do not edit files or create commits.
- Ground claims in repository evidence and cite exact paths and symbols.
- If the brief asks for changes, describe the recommended changes instead of applying them.
- Return the requested report as the final agent message.

User brief:
"""


class OffloadError(RuntimeError):
    """A conductor operation could not complete safely."""


@dataclass(frozen=True)
class WeftReceipt:
    job_id: str
    placement_decision: str
    selected_host: str
    source_pin: str
    accepted_immediately: bool
    deduplicated: bool
    idempotency_key: str

    @classmethod
    def parse(cls, text: str, *, idempotency_key: str) -> WeftReceipt:
        value = load_json_object(text, subject="Weft submission receipt")

        def text_field(name: str) -> str:
            field = value.get(name, "")
            if not isinstance(field, str):
                raise ProtocolError(f"Weft receipt {name} must be a string")
            return field

        if value.get("api_version") != WEFT_RECEIPT_SCHEMA:
            raise ProtocolError(f"unsupported Weft receipt: {value.get('api_version')!r}")
        decision = value.get("placement_decision")
        if decision not in {"accepted_immediately", "deduplicated", "not_accepted"}:
            raise ProtocolError(f"invalid Weft placement decision: {decision!r}")
        accepted = value.get("accepted_immediately")
        deduplicated = value.get("deduplicated", False)
        if not isinstance(accepted, bool) or not isinstance(deduplicated, bool):
            raise ProtocolError("Weft receipt acceptance fields must be booleans")
        received_key = text_field("idempotency_key")
        if received_key != idempotency_key:
            raise ProtocolError("Weft receipt did not echo the task idempotency key")
        receipt = cls(
            job_id=text_field("job_id"),
            placement_decision=str(decision),
            selected_host=text_field("selected_host"),
            source_pin=text_field("source_pin"),
            accepted_immediately=accepted,
            deduplicated=deduplicated,
            idempotency_key=received_key,
        )
        if decision == "not_accepted":
            if receipt.job_id or accepted or deduplicated:
                raise ProtocolError("not_accepted Weft receipt claims a durable job")
        elif not receipt.job_id or not receipt.source_pin:
            raise ProtocolError("accepted Weft receipt lacks a job ID or source pin")
        elif decision == "accepted_immediately" and not accepted:
            raise ProtocolError("accepted_immediately receipt denies immediate acceptance")
        elif decision == "deduplicated" and not deduplicated:
            raise ProtocolError("deduplicated receipt lacks its deduplication flag")
        return receipt

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": WEFT_RECEIPT_SCHEMA,
            "job_id": self.job_id,
            "placement_decision": self.placement_decision,
            "selected_host": self.selected_host,
            "source_pin": self.source_pin,
            "accepted_immediately": self.accepted_immediately,
            "deduplicated": self.deduplicated,
            "idempotency_key": self.idempotency_key,
        }


def run_process(
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(arguments),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        raise OffloadError(f"cannot run {arguments[0]!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise OffloadError(f"{arguments[0]!r} exceeded {timeout:g} seconds") from exc


def command_json(arguments: Sequence[str], *, timeout: float = 60) -> dict[str, Any]:
    completed = run_process(arguments, timeout=timeout)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        raise OffloadError(f"{' '.join(arguments[:3])} failed: {detail}")
    try:
        return load_json_object(completed.stdout, subject=f"{arguments[0]} output")
    except ProtocolError as exc:
        raise OffloadError(str(exc)) from exc


def doctor(*, host: str, provider: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if provider != SUPPORTED_PROVIDER:
        checks.append(
            {
                "name": "provider",
                "ok": False,
                "detail": f"only {SUPPORTED_PROVIDER} is supported",
            }
        )
        return {
            "schema_version": DOCTOR_SCHEMA,
            "host": host,
            "provider": provider,
            "available": False,
            "checks": checks,
        }

    try:
        listing = command_json(["weft", "host", "list", "--json"])
        hosts = listing.get("hosts") if listing.get("kind") == "host_list" else None
        row = next(
            (item for item in hosts or [] if isinstance(item, dict) and item.get("name") == host),
            None,
        )
        capabilities = row.get("capabilities", []) if isinstance(row, dict) else []
        required = {f"agent:{provider}", "tool:agent-offload", "tool:agent-execution"}
        missing = sorted(required - set(capabilities if isinstance(capabilities, list) else []))
        checks.append(
            {
                "name": "inventory",
                "ok": not missing and row is not None,
                "detail": "required capabilities declared"
                if not missing and row is not None
                else f"missing capabilities: {', '.join(missing) or 'host not found'}",
            }
        )
    except OffloadError as exc:
        checks.append({"name": "inventory", "ok": False, "detail": str(exc)})

    try:
        probe = command_json(
            ["agent-review", "capability", "probe", host, "--json"],
            timeout=45,
        )
        observations = probe.get("observations")
        observation = next(
            (
                item
                for item in observations or []
                if isinstance(item, dict) and item.get("label") == f"agent:{provider}"
            ),
            None,
        )
        state = observation.get("state") if isinstance(observation, dict) else None
        authenticated = state in {"locally_authenticated", "verified_live"}
        detail = observation.get("detail") if isinstance(observation, dict) else None
        checks.append(
            {
                "name": "authentication",
                "ok": authenticated,
                "state": state,
                "detail": detail
                or (f"provider state: {state}" if state else "authentication not observed"),
            }
        )
    except OffloadError as exc:
        checks.append({"name": "authentication", "ok": False, "detail": str(exc)})

    return {
        "schema_version": DOCTOR_SCHEMA,
        "host": host,
        "provider": provider,
        "available": all(bool(check["ok"]) for check in checks),
        "checks": checks,
    }


def submitter_session(task_id: str) -> str:
    for name in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "AGENT_SESSION_ID"):
        value = os.environ.get(name)
        if value:
            return value
    return f"agent-offload/v1/{task_id}"


def remote_command(
    *,
    task_id: str,
    selector: str,
    effort: str,
    prompt_sha256: str,
    source_sha256: str,
    harness_timeout: float,
    ctx_timeout: float,
) -> str:
    return shlex.join(
        [
            "python3",
            "src/agent_offload/weft_entry.py",
            "execute",
            "--task-id",
            task_id,
            "--provider",
            SUPPORTED_PROVIDER,
            "--mode",
            SUPPORTED_MODE,
            "--harness-selector",
            selector,
            "--effort",
            effort,
            "--prompt-payload",
            "agent-brief",
            "--expect-prompt-sha256",
            prompt_sha256,
            "--expect-protocol",
            str(WORKER_PROTOCOL_VERSION),
            "--expect-source-sha256",
            source_sha256,
            "--harness-timeout",
            str(harness_timeout),
            "--ctx-timeout",
            str(ctx_timeout),
        ]
    )


def recover_submission(task_id: str) -> WeftReceipt | None:
    listing = command_json(
        [
            "weft",
            "job",
            "list",
            "--project",
            "agent-offload",
            "--filter",
            task_id,
            "--all",
            "--all-hosts",
            "--format",
            "json",
            "--limit",
            "5",
        ]
    )
    if listing.get("kind") != "job_list":
        raise OffloadError("unsupported Weft job-list schema during submission recovery")
    jobs = [
        row
        for row in listing.get("jobs", [])
        if isinstance(row, dict)
        and task_id in str(row.get("command", ""))
        and row.get("project") == "agent-offload"
    ]
    if not jobs:
        return None
    if len(jobs) != 1:
        raise OffloadError(f"submission recovery found {len(jobs)} jobs for task {task_id}")
    row = jobs[0]
    job_id = row.get("job_id")
    host = row.get("host")
    if not isinstance(job_id, str) or not isinstance(host, str):
        raise OffloadError("submission recovery found a malformed job row")
    inspect = inspect_job(job_id)
    source_pin = inspect.get("source_pin", "")
    if not isinstance(source_pin, str):
        source_pin = ""
    return WeftReceipt(
        job_id=job_id,
        placement_decision="deduplicated",
        selected_host=host,
        source_pin=source_pin,
        accepted_immediately=False,
        deduplicated=True,
        idempotency_key=task_id,
    )


def build_prompt(brief: str) -> str:
    return REPORT_PREAMBLE + brief.rstrip() + "\n"


def submit(
    *,
    project: Path,
    brief_path: Path,
    host: str,
    selector: str,
    effort: str,
    harness_timeout: float,
    ctx_timeout: float,
) -> tuple[WeftReceipt, str, str]:
    try:
        brief = brief_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OffloadError(f"cannot read brief {brief_path}: {exc}") from exc
    if not brief.strip():
        raise OffloadError("brief is empty")
    prompt = build_prompt(brief)
    task_id = str(uuid.uuid4())
    prompt_sha256 = sha256_text(prompt)
    source_sha256 = str(worker_identity()["source_sha256"])
    command = remote_command(
        task_id=task_id,
        selector=selector,
        effort=effort,
        prompt_sha256=prompt_sha256,
        source_sha256=source_sha256,
        harness_timeout=harness_timeout,
        ctx_timeout=ctx_timeout,
    )
    result_artifact = f"outputs/agent-offload-result-{task_id}.json"
    report_artifact = f"outputs/agent-offload-report-{task_id}.md"
    with tempfile.TemporaryDirectory(prefix="agent-offload-") as directory:
        payload = Path(directory) / "agent-brief"
        payload.write_text(prompt, encoding="utf-8")
        arguments = [
            "weft",
            "run",
            "--host",
            host,
            "--agent",
            SUPPORTED_PROVIDER,
            "--require-capability",
            "tool:agent-offload",
            "--require-capability",
            "tool:agent-execution",
            "--tag",
            "inventory",
            "--tag",
            "agentic",
            "--project",
            "agent-offload",
            "--submitter-session",
            submitter_session(task_id),
            "--produces",
            result_artifact,
            "--produces",
            report_artifact,
            "--payload",
            f"agent-brief={payload}",
            "--idempotency-key",
            task_id,
            "--json",
            "-m",
            f"agent-offload report task {task_id}",
            command,
        ]
        completed = run_process(arguments, cwd=project, timeout=90)
    if completed.returncode == 0:
        try:
            receipt = WeftReceipt.parse(completed.stdout.strip(), idempotency_key=task_id)
        except ProtocolError as exc:
            recovered = recover_submission(task_id)
            if recovered is None:
                raise OffloadError(f"ambiguous Weft submission: {exc}") from exc
            receipt = recovered
    else:
        recovered = recover_submission(task_id)
        if recovered is None:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise OffloadError(f"Weft submission failed: {detail}")
        receipt = recovered
    if receipt.placement_decision == "not_accepted":
        raise OffloadError("Weft did not accept the task as a durable job")
    return receipt, task_id, prompt_sha256


def inspect_job(job_id: str) -> dict[str, Any]:
    return command_json(["weft", "job", "inspect", job_id, "--json"])


def artifact_text(job_id: str, artifact: str) -> str:
    completed = run_process(["weft", "artifact", "cat", job_id, artifact], timeout=90)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        raise OffloadError(f"cannot retrieve {artifact}: {detail}")
    return completed.stdout


def retrieve(
    job_id: str,
    *,
    expected_task_id: str | None = None,
    expected_prompt_sha256: str | None = None,
    expected_source_sha256: str | None = None,
    mark_processed: bool = True,
) -> dict[str, Any]:
    inspection = inspect_job(job_id)
    if inspection.get("project") != "agent-offload":
        raise OffloadError(f"job {job_id} is not an agent-offload job")
    status = inspection.get("status")
    if status not in TERMINAL_STATUSES:
        raise OffloadError(f"job {job_id} is {status!r}; retry retrieval after it finishes")
    produces = inspection.get("produces")
    if not isinstance(produces, list):
        raise OffloadError(f"job {job_id} has no versioned produces list")
    result_artifacts = [
        item for item in produces if isinstance(item, str) and RESULT_PATTERN.match(item)
    ]
    if len(result_artifacts) != 1:
        raise OffloadError(f"job {job_id} does not declare one agent-offload result")
    result_artifact = result_artifacts[0]
    match = RESULT_PATTERN.match(result_artifact)
    assert match is not None
    task_id = match.group(1)
    if expected_task_id is not None and task_id != expected_task_id:
        raise OffloadError("job result artifact task identifier does not match submission")
    report_artifact = f"outputs/agent-offload-report-{task_id}.md"
    if report_artifact not in produces:
        raise OffloadError(f"job {job_id} does not declare its report artifact")
    result_text = artifact_text(job_id, result_artifact)
    try:
        result_value = load_json_object(result_text, subject="agent-offload result artifact")
        result = validate_result(
            result_value,
            task_id=task_id,
            prompt_sha256=expected_prompt_sha256,
            source_sha256=expected_source_sha256,
        )
    except ProtocolError as exc:
        raise OffloadError(str(exc)) from exc
    report_text = artifact_text(job_id, report_artifact)
    if result["status"] == "completed":
        expected_report = str(result["report"]) + "\n"
    else:
        expected_report = failure_report(str(result["failure"]))
    if report_text != expected_report:
        raise OffloadError("report artifact does not match the validated result envelope")
    processed = False
    processing_error: str | None = None
    if mark_processed:
        completed = run_process(["weft", "job", "mark-processed", job_id])
        processed = completed.returncode == 0
        if not processed:
            processing_error = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or "weft job mark-processed returned no diagnostic"
            )
    return {
        "schema_version": RETRIEVAL_SCHEMA,
        "job_id": job_id,
        "task_id": task_id,
        "inspection": inspection,
        "result_artifact": result_artifact,
        "report_artifact": report_artifact,
        "result": result,
        "report": str(result["report"]) if result["status"] == "completed" else None,
        "diagnostic_report": report_text.rstrip("\n"),
        "marked_processed": processed,
        "mark_processed_error": processing_error,
    }


def wait_for_result(
    receipt: WeftReceipt,
    *,
    task_id: str,
    prompt_sha256: str,
    source_sha256: str,
    timeout: float,
    mark_processed: bool,
) -> dict[str, Any]:
    waiting = run_process(
        [
            "weft",
            "status",
            receipt.job_id,
            "--wait",
            "--wait-timeout",
            f"{timeout:g}s",
            "--json",
        ],
        timeout=timeout + 90,
    )
    if waiting.returncode == 2:
        raise OffloadError(
            f"job {receipt.job_id} is still running after {timeout:g} seconds; "
            f"resume with: agent-offload retrieve {receipt.job_id}"
        )
    if waiting.returncode not in {0, 1}:
        detail = waiting.stderr.strip() or waiting.stdout.strip() or "no diagnostic output"
        raise OffloadError(f"Weft could not wait for job {receipt.job_id}: {detail}")
    inspection = inspect_job(receipt.job_id)
    status = inspection.get("status")
    if status not in TERMINAL_STATUSES:
        raise OffloadError(
            f"Weft wait returned while job {receipt.job_id} was {status!r}; "
            f"resume with: agent-offload retrieve {receipt.job_id}"
        )
    run_process(["weft", "artifact", "sync", receipt.job_id], timeout=120)
    try:
        return retrieve(
            receipt.job_id,
            expected_task_id=task_id,
            expected_prompt_sha256=prompt_sha256,
            expected_source_sha256=source_sha256,
            mark_processed=mark_processed,
        )
    except OffloadError as exc:
        raise OffloadError(
            f"job {receipt.job_id} finished, but its artifacts are not retrievable: {exc}; "
            f"resume with: agent-offload retrieve {receipt.job_id}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-offload")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="submit a durable report-only task")
    run.add_argument("project", nargs="?", type=Path, default=Path.cwd())
    run.add_argument("--brief", type=Path, required=True)
    run.add_argument("--host", default="studio")
    run.add_argument("--provider", default=SUPPORTED_PROVIDER)
    run.add_argument("--mode", default=SUPPORTED_MODE)
    run.add_argument("--selector", choices=(SUPPORTED_SELECTOR,), default=SUPPORTED_SELECTOR)
    run.add_argument("--effort", choices=("low", "medium", "high"), default="medium")
    run.add_argument("--harness-timeout", type=float, default=900.0)
    run.add_argument("--ctx-timeout", type=float, default=90.0)
    run.add_argument("--wait-timeout", type=float, default=2400.0)
    run.add_argument("--detach", action="store_true")
    run.add_argument("--no-mark-processed", action="store_true")
    run.add_argument("--json", action="store_true")
    run.set_defaults(handler=cmd_run)

    retrieve_parser = subparsers.add_parser("retrieve", help="retrieve and validate a job")
    retrieve_parser.add_argument("job_id")
    retrieve_parser.add_argument("--no-mark-processed", action="store_true")
    retrieve_parser.add_argument("--json", action="store_true")
    retrieve_parser.set_defaults(handler=cmd_retrieve)

    status = subparsers.add_parser("status", help="show the versioned Weft job record")
    status.add_argument("job_id")
    status.set_defaults(handler=cmd_status)

    doctor_parser = subparsers.add_parser("doctor", help="check remote agent readiness")
    doctor_parser.add_argument("--host", default="studio")
    doctor_parser.add_argument("--provider", default=SUPPORTED_PROVIDER)
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.set_defaults(handler=cmd_doctor)
    return parser


def warn_if_unprocessed(retrieval: dict[str, Any]) -> None:
    error = retrieval.get("mark_processed_error")
    if error:
        print(
            f"warning: result validated, but Weft did not mark the job processed: {error}",
            file=sys.stderr,
        )


def cmd_run(args: argparse.Namespace) -> int:
    project = args.project.expanduser().resolve()
    brief = args.brief.expanduser().resolve()
    if not project.is_dir():
        raise OffloadError(f"project is not a directory: {project}")
    if args.mode != SUPPORTED_MODE:
        raise OffloadError("agent-offload currently supports report mode only")
    if not args.detach and args.wait_timeout <= 0:
        raise OffloadError("--wait-timeout must be positive")
    readiness = doctor(host=args.host, provider=args.provider)
    if not readiness["available"]:
        details = "; ".join(
            f"{check['name']}: {check['detail']}"
            for check in readiness["checks"]
            if not check["ok"]
        )
        raise OffloadError(f"remote agent is not ready: {details}")
    receipt, task_id, prompt_sha256 = submit(
        project=project,
        brief_path=brief,
        host=args.host,
        selector=args.selector,
        effort=args.effort,
        harness_timeout=args.harness_timeout,
        ctx_timeout=args.ctx_timeout,
    )
    if args.detach:
        output = {
            "schema_version": "agent-offload.submission/v1",
            "task_id": task_id,
            "prompt_sha256": prompt_sha256,
            "receipt": receipt.to_dict(),
        }
        print(json.dumps(output, sort_keys=True) if args.json else receipt.job_id)
        return 0
    retrieval = wait_for_result(
        receipt,
        task_id=task_id,
        prompt_sha256=prompt_sha256,
        source_sha256=str(worker_identity()["source_sha256"]),
        timeout=args.wait_timeout,
        mark_processed=not args.no_mark_processed,
    )
    warn_if_unprocessed(retrieval)
    envelope = {
        "schema_version": "agent-offload.run-result/v1",
        "receipt": receipt.to_dict(),
        "retrieval": retrieval,
    }
    if args.json:
        print(json.dumps(envelope, sort_keys=True))
    elif retrieval["result"]["status"] == "completed":
        print(retrieval["report"])
    else:
        raise OffloadError(str(retrieval["result"]["failure"]))
    return 0


def cmd_retrieve(args: argparse.Namespace) -> int:
    retrieval = retrieve(args.job_id, mark_processed=not args.no_mark_processed)
    warn_if_unprocessed(retrieval)
    if args.json:
        print(json.dumps(retrieval, sort_keys=True))
    elif retrieval["result"]["status"] == "completed":
        print(retrieval["report"])
    else:
        raise OffloadError(str(retrieval["result"]["failure"]))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(inspect_job(args.job_id), sort_keys=True, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    result = doctor(host=args.host, provider=args.provider)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        for check in result["checks"]:
            marker = "ok" if check["ok"] else "fail"
            print(f"{marker}: {check['name']}: {check['detail']}")
    return 0 if result["available"] else 1


def main(argv: list[str] | None = None) -> None:
    try:
        args = build_parser().parse_args(argv)
        code = args.handler(args)
    except (OffloadError, ProtocolError) as exc:
        print(f"agent-offload: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main(sys.argv[1:])
