"""User-facing durable coding tasks; Weft owns execution and artifact storage."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from fnmatch import fnmatchcase
from pathlib import Path

from agent_execution.identity import source_sha256
from agent_execution.omp_execution import OMP_WRITER_SELECTORS, validate_omp_transcript
from agent_execution.worker import WORKER_PROTOCOL_VERSION, WorkerResult

from .protocol import package_source_sha256
from .task_protocol import (
    EXCLUDED,
    LIST_SCHEMA,
    MAX_TOTAL,
    RESULT_SCHEMA,
    SCHEMA,
    SELECTORS_SCHEMA,
    TaskError,
    decode,
    digest,
    file_bytes,
    manifest,
    object_json,
    render_diff,
    result_path,
    safe_path,
    task_id,
    validate_changes,
)

DEFAULT_SELECTOR = "openai-codex/gpt-6-sol"


def state_root() -> Path:
    return (
        Path(os.environ.get("AGENT_OFFLOAD_STATE", Path.home() / ".local/state/agent-offload"))
        / "tasks"
    )


def run(argv: list[str], *, cwd: Path | None = None, timeout: float = 60) -> str:
    try:
        result = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TaskError(f"could not execute {argv[0]}: {error}") from error
    if result.returncode:
        raise TaskError(
            result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        )
    return result.stdout


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.replace(path)


@contextmanager
def locked(identity: str):
    directory = state_root() / task_id(identity)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TaskError("another operation owns this task; retry after it finishes") from error
        yield directory


@contextmanager
def project_locked(project: Path):
    directory = state_root() / ".project-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = digest(os.fsencode(project.resolve()))
    with (directory / identity).open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise TaskError(
                "another application owns this project; retry after it finishes"
            ) from error
        yield


def read_record(identity: str) -> dict:
    return object_json((state_root() / task_id(identity) / "task.json").read_text(), SCHEMA)


def submit(
    project: Path,
    brief: str,
    *,
    host: str,
    selector: str,
    timeout: float,
    check: list[str],
    identity: str | None = None,
) -> dict:

    identity = task_id(identity or uuid.uuid4().hex)
    project = project.expanduser().resolve(strict=True)
    if not project.is_dir() or not brief.strip():
        raise TaskError("a project directory and nonempty brief are required")
    if (
        selector not in OMP_WRITER_SELECTORS
        or not math.isfinite(timeout)
        or not 1 <= timeout <= 7200
    ):
        raise TaskError("unsupported writer selector or timeout (1–7200 seconds)")
    with locked(identity) as directory:
        if (directory / "task.json").exists():
            raise TaskError(f"task {identity} already exists; inspect it or use task resume")
        names = snapshot_names(project)
        request = {
            "schema_version": SCHEMA,
            "id": identity,
            "brief": brief,
            "selector": selector,
            "timeout": timeout,
            "check": check,
            "baseline": manifest(project, names),
            "execution_sha256": source_sha256(),
            "offload_sha256": package_source_sha256(),
        }
        payload = directory / "request.json"
        save(payload, request)
        record = {
            "schema_version": SCHEMA,
            "id": identity,
            "project": str(project),
            "host": host,
            "title": brief.splitlines()[0][:160],
            "created_at": time.time(),
            "status": "submitting",
            "job_id": None,
            "request_sha256": digest(payload.read_bytes()),
            "request": request,
        }
        # The identity and immutable request survive an interrupted submission.
        save(directory / "task.json", record)
        return send(record, directory)


def snapshot_names(project: Path) -> list[str]:
    """Project files that Weft's source snapshot will carry to the worker.

    The worker requires every baseline file to exist in its snapshot, so a file
    that Weft's source exclusions drop must not enter the baseline. Weft reports
    the exclusions it applies; each is a Go glob matched against every path
    component, as weft's shouldExclude does.
    """
    names = run(
        ["rg", "--files", "--hidden", "-g", "!.git", "-g", "!.jj"], cwd=project
    ).splitlines()
    inspected = json.loads(run(["weft", "sync", "inspect", str(project), "--json"], cwd=project))
    excludes = inspected.get("excludes") if isinstance(inspected, dict) else None
    if not isinstance(excludes, list) or not all(isinstance(p, str) for p in excludes):
        raise TaskError("weft sync inspect did not report the source exclusions")
    # Go spells a negated character class [^...]; fnmatch spells it [!...].
    patterns = [p.replace("[^", "[!") for p in excludes]
    return [
        name
        for name in names
        if not any(
            part.lower() in EXCLUDED or any(fnmatchcase(part, p) for p in patterns)
            for part in Path(name).parts
        )
    ]


def send(record: dict, directory: Path) -> dict:
    from .cli import WeftReceipt

    payload = directory / "request.json"
    if digest(payload.read_bytes()) != record["request_sha256"]:
        raise TaskError("retained request was modified")
    command = shlex.join(
        [
            "agent-offload-task-worker",
            "--task-id",
            record["id"],
            "--request-sha256",
            record["request_sha256"],
        ]
    )
    try:
        raw = run(
            [
                "weft",
                "run",
                "-C",
                ".",
                "--host",
                record["host"],
                "--agent",
                "omp",
                "--setup",
                "none",
                "--tag",
                "inventory",
                "--tag",
                "agent-offload-task",
                "--project",
                "agent-offload",
                "--idempotency-key",
                record["id"],
                "--output",
                result_path(record["id"]),
                "--produces",
                result_path(record["id"]),
                "--payload",
                f"coding-task={payload}",
                "--wall-time",
                f"{int(record['request']['timeout']) + 1020}s",
                "--json",
                "-m",
                record["title"],
                command,
            ],
            cwd=Path(record["project"]),
            timeout=180,
        )
        receipt = WeftReceipt.parse(raw, idempotency_key=record["id"])
        if not receipt.job_id:
            raise TaskError("Weft did not accept the task")
        record.update(
            job_id=receipt.job_id,
            host=receipt.selected_host,
            source_pin=receipt.source_pin,
            status="queued",
        )
        record.pop("error", None)
    except ValueError as error:
        record.update(status="submission_unknown", error=str(error))
        save(directory / "task.json", record)
        raise TaskError(
            f"submission unresolved for task {record['id']}: {error}; resume this identity"
        ) from error
    save(directory / "task.json", record)
    return summary(record)


def summary(record: dict) -> dict:
    return {
        key: record.get(key)
        for key in (
            "schema_version",
            "id",
            "project",
            "host",
            "title",
            "created_at",
            "status",
            "job_id",
            "error",
            "applied_at",
        )
    }


def status(record: dict) -> dict:
    row = summary(record)
    if record.get("applied_at"):
        row["status"] = "applied"
        return row
    if not record.get("job_id"):
        return row
    try:
        observed = json.loads(
            run(["weft", "job", "inspect", record["job_id"], "--json"], timeout=15)
        )
        if (
            not isinstance(observed, dict)
            or observed.get("id") != record["job_id"]
            or not isinstance(observed.get("status"), str)
        ):
            raise TaskError("Weft observation has no matching job identity/status")
        row["job_status"] = observed["status"]
        row["status"] = observed["status"]
        if observed["status"] in {"completed", "failed"}:
            row["status"] = retrieve(record, include_diff=False)["status"]
        row["observed_at"] = time.time()
    except (OSError, ValueError, TypeError, KeyError) as error:
        row.update(status="unknown", error=str(error))
    return row


def retrieve(record: dict, *, include_diff: bool = True) -> dict:
    if not record.get("job_id"):
        raise TaskError("task has no accepted job identity")
    raw = run(["weft", "artifact", "cat", record["job_id"], result_path(record["id"])])
    if len(raw.encode()) > MAX_TOTAL * 3:
        raise TaskError("task artifact exceeds size limit")
    result = object_json(raw, RESULT_SCHEMA)
    request = record["request"]
    if (
        result.get("id") != record["id"]
        or result.get("request_sha256") != record["request_sha256"]
        or result.get("offload_sha256") != request["offload_sha256"]
    ):
        raise TaskError("task result does not match the retained dispatch")
    if result.get("worker") is None:
        if (
            result.get("status") != "failed"
            or result.get("changes") != []
            or result.get("validation") is not None
            or not isinstance(result.get("report"), str)
        ):
            raise TaskError("invalid task worker failure")
        return {**result, "diff": "", "job_id": record["job_id"]}
    worker = WorkerResult.parse(json.dumps(result.get("worker")))
    if (
        worker.model_call_id != record["id"]
        or worker.provider != "omp"
        or worker.worker_protocol_version != WORKER_PROTOCOL_VERSION
        or worker.worker_source_sha256 != request["execution_sha256"]
    ):
        raise TaskError("worker identity differs from the retained dispatch")
    if worker.status == "completed":
        validate_omp_transcript(
            worker.omp_evidence,
            selector=request["selector"],
            policy="workspace-write-no-shell",
            cwd=worker.worker_cwd,
            prompt_sha256=digest(request["brief"].encode()),
        )
    if result.get("status") not in {"completed", "failed", "validation_failed"}:
        raise TaskError("invalid task outcome")
    if result["status"] == "completed" and worker.status != "completed":
        raise TaskError("task claims success for an unsuccessful worker")
    if request["check"] and result["status"] == "completed":
        validation = result.get("validation")
        if (
            not isinstance(validation, dict)
            or validation.get("command") != request["check"]
            or validation.get("exit_code") != 0
        ):
            raise TaskError("task lacks successful requested validation")
    changes = validate_changes(request, result.get("changes"))
    return {
        **result,
        "diff": render_diff(changes) if include_diff else "",
        "job_id": record["job_id"],
    }


def apply_changes(root: Path, changes: list[dict], backup: Path) -> None:
    # Preflight every path before replacing any file. Existing bytes are kept
    # in the task journal so application failures can be rolled back.
    for change in changes:
        path = safe_path(root, change["path"])
        before = decode(change["before"]) if change["before"] is not None else None
        actual = file_bytes(path) if path.exists() else None
        if actual != before or (
            before is not None and path.stat().st_mode & 0o777 != change["before_mode"]
        ):
            raise TaskError(f"local file changed since submission: {change['path']}")
    save(backup, {"changes": changes})
    replaced = []
    try:
        for change in changes:
            path = safe_path(root, change["path"])
            if change["after"] is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                    stream.write(decode(change["after"]))
                    temporary = Path(stream.name)
                temporary.chmod(change["after_mode"])
                temporary.replace(path)
            replaced.append(change)
    except OSError:
        for change in reversed(replaced):
            path = safe_path(root, change["path"])
            if change["before"] is None:
                path.unlink()
            else:
                path.write_bytes(decode(change["before"]))
                path.chmod(change["before_mode"])
        raise


def selectors() -> dict:
    return {
        "schema_version": SELECTORS_SCHEMA,
        "default": DEFAULT_SELECTOR,
        "selectors": sorted(OMP_WRITER_SELECTORS),
    }


def handler(args: argparse.Namespace) -> int:
    try:
        if args.action == "selectors":
            value = selectors()
            if not args.json:
                print("\n".join(value["selectors"]))
                return 0
        elif args.action == "submit":
            brief = args.prompt if args.prompt is not None else args.brief.read_text()
            value = submit(
                args.project,
                brief,
                host=args.host,
                selector=args.selector,
                timeout=args.timeout,
                check=shlex.split(args.check or ""),
                identity=args.id,
            )
        elif args.action == "list":
            records = []
            for path in sorted(state_root().glob("*/task.json")):
                try:
                    record = object_json(path.read_text(), SCHEMA)
                    if any(
                        not isinstance(record.get(key), str)
                        for key in ("id", "project", "host", "title", "status")
                    ):
                        raise TaskError("task record lacks required text fields")
                    records.append(record)
                except (OSError, ValueError) as error:
                    records.append(
                        {
                            "schema_version": SCHEMA,
                            "id": path.parent.name,
                            "project": "",
                            "host": "unknown",
                            "title": "Unreadable task record",
                            "status": "unknown",
                            "job_id": None,
                            "error": f"{path}: {error}",
                        }
                    )
            with ThreadPoolExecutor(max_workers=4) as pool:
                rows = list(pool.map(status, records))
            value = {"schema_version": LIST_SCHEMA, "tasks": rows}
        else:
            with locked(args.id) as directory:
                record = read_record(args.id)
                if args.action == "status":
                    value = status(record)
                elif args.action == "resume":
                    if record.get("job_id"):
                        value = status(record)
                    else:
                        # A new source snapshot must not silently replace the
                        # baseline of a submission whose acceptance is unknown.
                        if (
                            manifest(Path(record["project"]), list(record["request"]["baseline"]))
                            != record["request"]["baseline"]
                        ):
                            raise TaskError(
                                "project changed during unresolved submission; inspect Weft before resuming"
                            )
                        value = send(record, directory)
                elif args.action == "cancel":
                    if not record.get("job_id"):
                        raise TaskError("resolve submission before cancellation")
                    run(["weft", "cancel", record["job_id"]])
                    value = status(record)
                else:
                    value = retrieve(record)
                    if args.action == "diff":
                        print(value["diff"])
                        return 0
                    if args.action == "apply":
                        if record.get("applied_at"):
                            raise TaskError("task was already applied")
                        if value["status"] != "completed":
                            raise TaskError("only successful tasks can be applied")
                        project = Path(record["project"])
                        with project_locked(project):
                            apply_changes(
                                project,
                                value["changes"],
                                directory / "application-backup.json",
                            )
                            record.update(applied_at=time.time(), status="applied")
                            save(directory / "task.json", record)
                        value = summary(record)
        print(json.dumps(value, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"agent-offload task: {error}", file=__import__("sys").stderr)
        return 1


def build_task_parser(subparsers) -> None:
    task = subparsers.add_parser("task", help="durable remote coding tasks")
    actions = task.add_subparsers(dest="action", required=True)
    submit_parser = actions.add_parser("submit")
    submit_parser.add_argument("project", type=Path)
    brief = submit_parser.add_mutually_exclusive_group(required=True)
    brief.add_argument("--brief", type=Path)
    brief.add_argument("--prompt")
    submit_parser.add_argument("--host", default="studio")
    submit_parser.add_argument(
        "--selector", choices=sorted(OMP_WRITER_SELECTORS), default=DEFAULT_SELECTOR
    )
    submit_parser.add_argument("--timeout", type=float, default=1800)
    submit_parser.add_argument("--check", help="validation argv, shell-quoted; no shell expansion")
    submit_parser.add_argument("--id", help="caller-generated idempotency identity")
    submit_parser.set_defaults(handler=handler)
    selectors_parser = actions.add_parser(
        "selectors", help="list the writer selectors this installation accepts"
    )
    selectors_parser.add_argument("--json", action="store_true")
    selectors_parser.set_defaults(handler=handler)
    actions.add_parser("list").set_defaults(handler=handler)
    for action in ("status", "resume", "cancel", "retrieve", "diff", "apply"):
        item = actions.add_parser(action)
        item.add_argument("id")
        item.set_defaults(handler=handler)
