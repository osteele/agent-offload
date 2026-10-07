"""User-facing durable coding tasks; Weft owns execution and artifact storage."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
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
from .readiness import require_writer
from .task_protocol import (
    EXCLUDED,
    LIST_SCHEMA,
    MAX_FILE,
    MAX_TOTAL,
    RESULT_SCHEMA,
    SCHEMA,
    SELECTORS_SCHEMA,
    TaskError,
    census,
    decode,
    digest,
    file_bytes,
    manifest,
    object_json,
    render_diff,
    result_path,
    safe_path,
    split_carried,
    task_id,
    validate_changes,
)

DEFAULT_SELECTOR = "openai-codex/gpt-6-luna"
# Allow the updater's 45-minute source-lock wait plus time for a cold build.
ENSURE_TIMEOUT = 3600
ENSURE_SCHEMA = "agent-host-sync.ensure/v1"
WORKSPACE_LANE = "agent-host-sync workspace run --dependency <path> ... <command>"
# Seconds one `weft run` may take to record the job. Submission passes
# --no-sync, so the budget covers source pinning and recording, not the host
# push; `send` resolves a timeout through `weft job lookup` rather than failing.
SUBMIT_TIMEOUT = 180
# Weft statuses of a job that ended without completing. A job that ends this
# way and publishes no result is resumable; a canceled job is not, because the
# consumer asked for it to stop.
UNFINISHED_JOB_STATUSES = {"failed", "dead", "killed"}
TERMINAL_JOB_STATUSES = {"completed", "failed", "dead", "canceled", "cancelled", "killed"}
WEFT_LOOKUP_SCHEMA = "weft.job.lookup.v1"


class ResultUnavailable(TaskError):
    """Weft could not supply the task's result artifact."""


def state_root() -> Path:
    return (
        Path(os.environ.get("AGENT_OFFLOAD_STATE", Path.home() / ".local/state/agent-offload"))
        / "tasks"
    )


class CommandTimeout(TaskError):
    """A command outlived its budget, so whatever it did before being killed is unknown."""


def run(
    argv: list[str], *, cwd: Path | None = None, timeout: float = 60, accept: tuple[int, ...] = ()
) -> str:
    """Run a command and return its stdout.

    `accept` holds the documented nonzero exits a caller permits -- rg's
    "no match" census result, say. Every other nonzero exit, and any failure
    to start or to finish within budget, still raises.
    """
    try:
        result = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise CommandTimeout(f"could not execute {argv[0]}: {error}") from error
    except OSError as error:
        raise TaskError(f"could not execute {argv[0]}: {error}") from error
    if result.returncode and result.returncode not in accept:
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


def task_title(brief: str) -> str:
    """Use the first non-blank brief line as a readable Weft description."""
    first = next((line.strip() for line in brief.splitlines() if line.strip()), "")
    title = re.sub(r"^#+\s+", "", first)
    title = re.sub(r"^Task:\s*", "", title, flags=re.IGNORECASE).strip()
    return (title or first or "Untitled task")[:160]


def submit(
    project: Path,
    brief: str,
    *,
    host: str,
    selector: str,
    timeout: float,
    check: list[str],
    account: str = "agent",
    identity: str | None = None,
) -> dict:

    identity = task_id(identity or uuid.uuid4().hex)
    project = project.expanduser().resolve(strict=True)
    if not project.is_dir() or not brief.strip():
        raise TaskError("a project directory and nonempty brief are required")
    if selector not in OMP_WRITER_SELECTORS:
        permitted = ", ".join(sorted(OMP_WRITER_SELECTORS))
        raise TaskError(f"writer selector {selector!r} is not admitted; permitted: {permitted}")
    if not math.isfinite(timeout) or not 1 <= timeout <= 7200:
        raise TaskError(f"timeout {timeout!r} is outside the permitted range (1-7200 seconds)")
    # The writer cannot execute anything (workspace-write-no-shell), so only a
    # supplied check builds remotely and only it can hit the missing dependency.
    stale = escaping_path_dependencies(project) if check else []
    if stale:
        raise TaskError(
            "project declares path dependencies outside the snapshot "
            f"({', '.join(stale)}); the worker receives only the project directory, "
            "so the check would fail remotely after the writer has finished. Use "
            f"{WORKSPACE_LANE} for projects with external path dependencies"
        )
    # The immutable build returned by ensure_worker must match both bundled
    # package digests. A separate execution worker on PATH does not run this task.
    with locked(identity) as directory:
        if (directory / "task.json").exists():
            raise TaskError(f"task {identity} already exists; inspect it or use task resume")
        editable, carried = split_carried(project, snapshot_names(project))
        request = {
            "schema_version": SCHEMA,
            "id": identity,
            "brief": brief,
            "selector": selector,
            "timeout": timeout,
            "check": check,
            "baseline": manifest(project, editable),
            "carried": census(project, carried),
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
            "account": account,
            "title": task_title(brief),
            "created_at": time.time(),
            "status": "submitting",
            "job_id": None,
            "request_sha256": digest(payload.read_bytes()),
            "request": request,
        }
        # The identity and immutable request survive an interrupted submission.
        save(directory / "task.json", record)
        ensure_worker(record)
        save(directory / "task.json", record)
        return send(record, directory)


def ensure_worker(record: dict) -> None:
    """Build the exact peer for this dispatch without changing the host's live tool."""
    try:
        raw = run(
            [
                "agent-host-sync",
                "--host",
                record["host"],
                "--account",
                record.get("account", "agent"),
                "ensure",
                "--tool",
                "agent-offload",
                "--json",
            ],
            timeout=ENSURE_TIMEOUT,
        )
    except TaskError as error:
        detail = str(error).strip()
        raise TaskError(f"agent-host-sync ensure failed: {detail[-2000:]}") from error
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise TaskError("agent-host-sync ensure returned invalid JSON") from error
    if not isinstance(value, dict) or value.get("schema") != ENSURE_SCHEMA:
        raise TaskError(f"agent-host-sync ensure did not return {ENSURE_SCHEMA}")
    builds = value.get("builds")
    peers = (
        [item for item in builds if isinstance(item, dict) and item.get("tool") == "agent-offload"]
        if isinstance(builds, list)
        else []
    )
    if len(peers) != 1:
        raise TaskError("agent-host-sync ensure must return exactly one agent-offload build")
    peer = peers[0]
    identity = peer.get("identity")
    build_id, bin_dir = peer.get("build_id"), peer.get("bin_dir")
    if (
        not isinstance(identity, dict)
        or not isinstance(identity.get("source_sha256"), str)
        or not isinstance(identity.get("execution"), dict)
        or not isinstance(identity["execution"].get("source_sha256"), str)
        or not isinstance(build_id, str)
        or not re.fullmatch(r"[0-9a-f]{64}", build_id)
        or not isinstance(bin_dir, str)
        or not Path(bin_dir).is_absolute()
    ):
        raise TaskError("agent-host-sync ensure returned a malformed agent-offload build")
    request = record["request"]
    mismatches = []
    if identity["source_sha256"] != request["offload_sha256"]:
        mismatches.append("agent-offload")
    if identity["execution"]["source_sha256"] != request["execution_sha256"]:
        mismatches.append("agent-execution")
    if mismatches:
        sides = " and ".join(mismatches)
        raise TaskError(
            f"remote {sides} identity differs from the task dispatch. The local installed "
            "agent-offload was built from different bytes than the agent-offload / "
            "agent-execution checkouts that agent-host-sync deploys. Reinstall agent-offload "
            "locally (`just install` in the agent-offload checkout), or commit/revert the "
            "checkout edits, then resubmit."
        )
    record["bin_dir"] = bin_dir
    record["build_id"] = build_id


def swiftpm_path_dependencies(source: str) -> list[str]:
    """The ``path`` argument of every ``.package(...)`` dependency in a manifest."""
    # A commented-out dependency must not refuse a valid project.
    source = re.sub(r"//[^\n]*", "", source)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    declared = []
    position = 0
    while (call := source.find(".package(", position)) != -1:
        depth = 1
        end = call + len(".package(")
        while end < len(source) and depth:
            if source[end] == "(":
                depth += 1
            elif source[end] == ")":
                depth -= 1
            end += 1
        body = source[call + len(".package(") : end - 1 if depth == 0 else end]
        declared.extend(re.findall(r'\bpath:\s*"([^"]+)"', body))
        position = end
    return declared


def pyproject_path_dependencies(text: str) -> list[str]:
    """Path dependencies declared in a pyproject.toml.

    The package supports Python 3.10, where tomllib is unavailable, so this
    covers the two local spellings textually: ``[tool.uv.sources]`` path
    entries and ``name @ file://`` requirements. Commented lines are skipped,
    and unexpanded placeholders such as ``${PROJECT_ROOT}`` are skipped
    because their containment cannot be judged locally.
    """
    declared = []
    in_sources = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_sources = stripped == "[tool.uv.sources]"
            continue
        if in_sources:
            declared.extend(re.findall(r'\bpath\s*=\s*"([^"]+)"', stripped))
        declared.extend(re.findall(r'"[^"]*@ file://([^"]+)"', stripped))
    return [entry for entry in declared if "${" not in entry]


def escaping_path_dependencies(project: Path) -> list[str]:
    """Declared local path dependencies the project-only source snapshot cannot carry.

    The worker builds inside a snapshot of the project directory alone, so a
    SwiftPM ``.package(path:)`` or a pyproject path source resolving outside it
    is missing remotely and fails the check after the writer has finished.
    Dependencies inside the project travel with the snapshot.
    """
    declared: list[str] = []
    package = project / "Package.swift"
    if package.is_file():
        declared.extend(swiftpm_path_dependencies(package.read_text()))
    pyproject = project / "pyproject.toml"
    if pyproject.is_file():
        declared.extend(pyproject_path_dependencies(pyproject.read_text()))
    return [entry for entry in declared if not (project / entry).resolve().is_relative_to(project)]


def snapshot_names(project: Path) -> list[str]:
    """Project files that Weft's source snapshot will carry to the worker.

    The worker requires every baseline file to exist in its snapshot, so a file
    that Weft's source exclusions drop must not enter the baseline. Root-anchored
    patterns match a path or its ancestors; other patterns match individual
    path components, as Weft's shouldExclude does.
    """
    # rg exits 1 when it lists nothing, its documented "no match" result, so
    # an empty project is a valid census rather than a failure. Only that exit
    # is permitted here: a missing rg or any other census failure still raises
    # before a task record exists.
    names = run(
        ["rg", "--files", "--hidden", "-g", "!.git", "-g", "!.jj"],
        cwd=project,
        accept=(1,),
    ).splitlines()
    inspected = json.loads(run(["weft", "sync", "inspect", str(project), "--json"], cwd=project))
    excludes = inspected.get("excludes") if isinstance(inspected, dict) else None
    if not isinstance(excludes, list) or not all(isinstance(p, str) for p in excludes):
        raise TaskError("weft sync inspect did not report the source exclusions")
    # Go spells a negated character class [^...]; fnmatch spells it [!...].
    patterns = [p.replace("[^", "[!") for p in excludes]
    anchored = [p[1:].split("/") for p in patterns if p.startswith("/")]
    unanchored = [p for p in patterns if not p.startswith("/")]

    def excluded(path: Path) -> bool:
        parts = path.parts
        return any(
            part.lower() in EXCLUDED or any(fnmatchcase(part, p) for p in unanchored)
            for part in parts
        ) or any(
            len(pattern) <= len(parts)
            and all(fnmatchcase(part, p) for part, p in zip(parts, pattern))
            for pattern in anchored
        )

    links, oversized = snapshot_link_problems(project, excluded)
    if links:
        raise TaskError(
            f"project contains symlinks the Weft snapshot carries ({listing(links)}); the task "
            "worker refuses symlinks, so the task would fail remotely before the writer "
            "started. Replace them with regular files or exclude them from the snapshot"
        )
    if oversized:
        raise TaskError(
            f"symlinks leaving the project reach files over the {MAX_FILE // 2**20} MiB task "
            f"file limit ({listing(oversized)}); Weft copies their content into the snapshot "
            "and the task worker refuses it, so the task would fail remotely before the writer "
            "started. Exclude them from the snapshot"
        )
    return [name for name in names if not excluded(Path(name))]


def listing(names: list[str]) -> str:
    return ", ".join(names[:5]) + (f", and {len(names) - 5} more" if len(names) > 5 else "")


def snapshot_link_problems(project: Path, excluded) -> tuple[list[str], list[str]]:
    """Symlinks the task worker would refuse, as Weft's source snapshot carries them.

    rg does not list symlinks, so they never enter the baseline. Weft keeps
    every in-root link that its exclusions do not drop, and the worker's census
    refuses any symlink it finds; those are the first list. Weft replaces a
    link whose target is absolute or leaves the tree with the target's content,
    as its symlinkTargetWithinRoot does, applying the same exclusions inside it
    and following further links. The census refuses any file of that content
    over the task file limit; those, by their snapshot paths, are the second.
    """
    in_root: list[str] = []
    oversized: list[str] = []
    for directory, dirs, files in os.walk(project):
        relative = Path(directory).relative_to(project)
        for name in sorted([*dirs, *files]):
            path = Path(directory) / name
            if excluded(relative / name) or not path.is_symlink():
                continue
            target = os.readlink(path)
            joined = os.path.normpath(os.path.join(relative, target))
            snapshot_name = (relative / name).as_posix()
            if not os.path.isabs(target) and joined != ".." and not joined.startswith("../"):
                in_root.append(snapshot_name)
            else:
                oversized.extend(oversized_content(path, snapshot_name, excluded))
        dirs[:] = [d for d in dirs if not excluded(relative / d)]
    return in_root, oversized


def oversized_content(path: Path, snapshot_name: str, excluded) -> list[str]:
    """Snapshot paths of files over the task file limit at or under path."""
    if path.is_file():
        return [snapshot_name] if path.stat().st_size > MAX_FILE else []
    found = []
    # Links inside the target are followed, so a link back to an ancestor
    # would repeat forever; each real directory is walked once, as Weft does.
    visited = set()
    for directory, dirs, files in os.walk(path, followlinks=True):
        visited.add(os.path.realpath(directory))
        relative = Path(snapshot_name) / Path(directory).relative_to(path)
        dirs[:] = sorted(
            d
            for d in dirs
            if not excluded(relative / d) and os.path.realpath(Path(directory) / d) not in visited
        )
        for name in sorted(files):
            file = Path(directory) / name
            if not excluded(relative / name) and file.is_file() and file.stat().st_size > MAX_FILE:
                found.append((relative / name).as_posix())
    return found


def send(record: dict, directory: Path) -> dict:
    from .cli import WeftReceipt

    payload = directory / "request.json"
    if digest(payload.read_bytes()) != record["request_sha256"]:
        raise TaskError("retained request was modified")
    # A send that reached `weft run` may already have been accepted, whether it
    # ended unresolved or the process died before persisting the receipt.
    # Reconcile the durable receipt before provisioning or checking today's
    # credentials/model availability; never resend an accepted task merely
    # because readiness has since changed. A failed or malformed lookup is not
    # evidence of absence and leaves the submission unresolved.
    if record.get("status") == "submission_unknown" or record.get("submission_started"):
        try:
            recovered = recorded_receipt(record, Path(record["project"]))
        except TaskError as error:
            raise TaskError(
                f"submission for task {record['id']} remains unresolved: {error}; "
                f"resume with: agent-offload task resume {record['id']}"
            ) from error
        if recovered is not None:
            try:
                receipt = WeftReceipt.parse(recovered, idempotency_key=idempotency_key(record))
            except ValueError as error:
                raise TaskError(
                    f"submission receipt for task {record['id']} is corrupt: {error}"
                ) from error
            if not receipt.job_id:
                raise TaskError(f"Weft did not accept task {record['id']}")
            record.update(
                job_id=receipt.job_id,
                host=receipt.selected_host,
                source_pin=receipt.source_pin,
                status="queued",
            )
            record.pop("error", None)
            save(directory / "task.json", record)
            return summary(record)
    if not record.get("bin_dir") or not record.get("build_id"):
        ensure_worker(record)
        save(directory / "task.json", record)
    try:
        require_writer(record)
    except TaskError as error:
        raise TaskError(
            f"writer readiness failed for retained task {record['id']}: {error}; "
            f"resume with: agent-offload task resume {record['id']}"
        ) from error
    command = shlex.join(
        [
            str(Path(record["bin_dir"]) / "agent-offload-task-worker"),
            "--task-id",
            record["id"],
            "--request-sha256",
            record["request_sha256"],
        ]
    )
    submission = [
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
        "--idempotency-key",
        idempotency_key(record),
        "--output",
        result_path(record["id"]),
        "--produces",
        result_path(record["id"]),
        "--payload",
        f"coding-task={payload}",
        "--wall-time",
        f"{int(record['request']['timeout']) + 1020}s",
        "--no-sync",
        "--json",
        "-m",
        record["title"],
        command,
    ]
    project = Path(record["project"])
    # Durable before Weft can accept anything under this attempt's key, so a
    # process killed after acceptance but before the receipt is saved still
    # resumes by looking the key up rather than by probing readiness first.
    record["submission_started"] = True
    save(directory / "task.json", record)
    try:
        try:
            raw = run(submission, cwd=project, timeout=SUBMIT_TIMEOUT)
        except CommandTimeout:
            # The timeout killed weft run, so no submission under this key is
            # still in flight and the lookup's answer is conclusive. A recorded
            # job is bound; an unrecorded one is submitted once more.
            raw = recorded_receipt(record, project)
            if raw is None:
                raw = run(submission, cwd=project, timeout=SUBMIT_TIMEOUT)
        receipt = WeftReceipt.parse(raw, idempotency_key=idempotency_key(record))
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


def recorded_receipt(record: dict, project: Path) -> str | None:
    """The receipt of the job Weft recorded under the record's key, or None if none was.

    A lookup that fails raises rather than returning None: an unreadable ledger
    is not evidence that the job is absent.
    """
    key = idempotency_key(record)
    raw = run(["weft", "job", "lookup", "--idempotency-key", key], cwd=project, timeout=60)
    try:
        found = json.loads(raw)
    except json.JSONDecodeError as error:
        raise TaskError("weft job lookup returned invalid JSON") from error
    if (
        not isinstance(found, dict)
        or found.get("api_version") != WEFT_LOOKUP_SCHEMA
        or found.get("idempotency_key") != key
        or not isinstance(found.get("found"), bool)
    ):
        raise TaskError(f"weft job lookup did not return a {WEFT_LOOKUP_SCHEMA} answer for {key}")
    if not found["found"]:
        return None
    receipt = found.get("receipt")
    if not isinstance(receipt, dict):
        raise TaskError("weft job lookup found the job but returned no receipt")
    return json.dumps(receipt)


def idempotency_key(record: dict) -> str:
    """The Weft idempotency key of the record's current submission attempt."""
    attempt = record.get("attempt", 1)
    return record["id"] if attempt == 1 else f"{record['id']}-{attempt}"


def check_baseline_unchanged(record: dict) -> None:
    # A new source snapshot must not silently replace the retained baseline,
    # nor the carried files the writer reads and the check builds against. A
    # request retained before carried files were admitted has no carried set,
    # and could have none: submit refused any project holding one outright.
    project, request = Path(record["project"]), record["request"]
    carried = request.get("carried", {})
    if manifest(project, list(request["baseline"])) != request["baseline"] or (
        census(project, list(carried)) != carried
    ):
        raise TaskError("project changed since submission; inspect Weft before resuming")


def check_submitter_unchanged(record: dict) -> None:
    changed = []
    for name, key, current in (
        ("agent-offload", "offload_sha256", package_source_sha256()),
        ("agent-execution", "execution_sha256", source_sha256()),
    ):
        retained = record["request"][key]
        if retained != current:
            changed.append(
                f"{name} digest changed (retained {retained[:12]}, current {current[:12]})"
            )
    if changed:
        raise TaskError(
            f"{'; '.join(changed)}; a retained request cannot be resumed after the submitting "
            "code changed. Submit a new task for the same brief"
        )


def resubmit(record: dict, directory: Path) -> dict:
    """Submit the retained request again after its job failed without a result."""
    check_baseline_unchanged(record)
    check_submitter_unchanged(record)
    ensure_worker(record)
    # Dispose the old attempt before rotating its identity; a failed
    # acknowledgment must not create a second submission.
    acknowledge(record, directory)
    record.update(
        prior_job_ids=[*record.get("prior_job_ids", []), record["job_id"]],
        attempt=record.get("attempt", 1) + 1,
        job_id=None,
        status="submitting",
    )
    # The new attempt's key has not been sent yet; the earlier attempt's
    # started marker must not make it look potentially accepted.
    record.pop("processed_at", None)
    record.pop("submission_started", None)
    # A new attempt key lets Weft accept a second job; it survives interruption.
    save(directory / "task.json", record)
    return send(record, directory)


def unpublished_failure(record: dict, observed: dict, error: ResultUnavailable) -> str:
    try:
        log = run(["weft", "job", "log", record["job_id"], "-n", "20"], timeout=15).strip()
    except TaskError as log_error:
        log = f"(job log unavailable: {log_error})"
    return (
        f"job {record['job_id']} ended {observed['status']} with exit code "
        f"{observed.get('exit_code')} "
        f"without publishing a task result ({error}). Job log tail:\n{log}\n"
        "Submission provisions the matching worker on the host, so a version mismatch here "
        "means the pinned build changed or disappeared under the queued job; "
        f"`agent-offload task resume {record['id']}` provisions it again and resubmits. "
        "If the submitting machine's agent-offload or agent-execution changed since "
        "submission, resume refuses the resubmission; submit a new task instead."
    )


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
            "prior_job_ids",
            "processed_at",
        )
    }


def observe_job(record: dict) -> dict:
    """Inspect the task's own Weft job and validate the returned identity."""
    observed = json.loads(run(["weft", "job", "inspect", record["job_id"], "--json"], timeout=15))
    if (
        not isinstance(observed, dict)
        or observed.get("id") != record["job_id"]
        or not isinstance(observed.get("status"), str)
    ):
        raise TaskError("Weft observation has no matching job identity/status")
    return observed


def acknowledge(record: dict, directory: Path) -> None:
    """Mark a terminal job processed, recording success only after Weft accepts it."""
    if record.get("processed_at"):
        return
    observed = observe_job(record)
    if observed["status"] not in TERMINAL_JOB_STATUSES:
        raise TaskError(
            f"job {record['job_id']} is {observed['status']!r}; only a job Weft "
            "reports finished can be marked processed"
        )
    try:
        run(["weft", "job", "mark-processed", record["job_id"]], timeout=15)
    except TaskError as error:
        raise TaskError(
            f"weft job mark-processed failed for {record['job_id']}: {error}; "
            "repeat this command to retry the acknowledgment"
        ) from error
    record["processed_at"] = time.time()
    save(directory / "task.json", record)


def status(record: dict) -> dict:
    row = summary(record)
    if record.get("applied_at"):
        row["status"] = "applied"
        return row
    if not record.get("job_id"):
        return row
    try:
        observed = observe_job(record)
        row["job_status"] = observed["status"]
        row["status"] = observed["status"]
        if observed["status"] == "completed" or observed["status"] in UNFINISHED_JOB_STATUSES:
            try:
                row["status"] = retrieve(record, include_diff=False)["status"]
            except ResultUnavailable as error:
                publication = observed.get("publication")
                published = (
                    isinstance(publication, dict)
                    and publication.get("required_artifacts_state") == "ready"
                )
                # A completed job, or one whose result Weft reports as ready,
                # should have a result; failing to read it is not a job failure.
                if observed["status"] == "completed" or published:
                    raise
                row.update(
                    status="failed_without_result",
                    error=unpublished_failure(record, observed, error),
                )
        row["observed_at"] = time.time()
    except (OSError, ValueError, TypeError, KeyError) as error:
        row.update(status="unknown", error=str(error))
    return row


def retrieve(record: dict, *, include_diff: bool = True) -> dict:
    if not record.get("job_id"):
        raise TaskError("task has no accepted job identity")
    try:
        raw = run(["weft", "artifact", "cat", record["job_id"], result_path(record["id"])])
    except TaskError as error:
        raise ResultUnavailable(str(error)) from error
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
        # Strict provenance pinning is the default, ratified 2026-09-26: a dispatch
        # is bound to the exact tooling bytes that produced it, and a mismatch is
        # refused rather than warned about. The rejected alternative -- warn-and-
        # continue -- would convert the provenance guarantee behind task
        # retrieve/apply into a best-effort label that every downstream consumer
        # inherits. Unlike the earlier gates, this refusal is a hard error at
        # retrieve time: a mismatched result is never accepted.
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
    validation = result.get("validation")
    if not request["check"] and validation is not None:
        raise TaskError("task reports validation that was not requested")
    if result["status"] == "validation_failed" and (
        not request["check"] or not isinstance(validation, dict)
    ):
        raise TaskError("task reports a validation failure without requested validation")
    if (
        request["check"]
        and result["status"] == "completed"
        and (
            not isinstance(validation, dict)
            or validation.get("command") != request["check"]
            or validation.get("exit_code") != 0
        )
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
    created: list[Path] = []
    try:
        for change in changes:
            path = safe_path(root, change["path"])
            if change["after"] is None:
                path.unlink()
            else:
                missing = [p for p in path.parents if p.is_relative_to(root) and not p.exists()]
                for directory in reversed(missing):
                    directory.mkdir()
                    created.append(directory)
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    try:
                        stream.write(decode(change["after"]))
                        stream.close()
                        temporary.chmod(change["after_mode"])
                        temporary.replace(path)
                    except OSError:
                        temporary.unlink(missing_ok=True)
                        raise
            replaced.append(change)
    except (OSError, TaskError):
        # TaskError covers a path that turned into a symlink after preflight.
        for change in reversed(replaced):
            path = safe_path(root, change["path"])
            if change["before"] is None:
                path.unlink()
            else:
                path.write_bytes(decode(change["before"]))
                path.chmod(change["before_mode"])
        for directory in reversed(created):
            directory.rmdir()
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
                account=args.account,
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
                        if value["status"] == "failed_without_result":
                            value = resubmit(record, directory)
                    else:
                        # An unresolved submission may already be queued under
                        # its idempotency key; resending must repeat the same
                        # command, so the recorded build is kept. send provisions
                        # one only for a record that never had one.
                        check_baseline_unchanged(record)
                        value = send(record, directory)
                elif args.action == "cancel":
                    if not record.get("job_id"):
                        raise TaskError("resolve submission before cancellation")
                    observed = observe_job(record)
                    if observed["status"] not in TERMINAL_JOB_STATUSES:
                        run(["weft", "cancel", record["job_id"]])
                    # Cancellation may be asynchronous; acknowledge only after
                    # a second observation confirms the job is terminal.
                    acknowledge(record, directory)
                    value = status(record)
                else:
                    if args.action != "apply":
                        value = retrieve(record)
                        if args.action == "diff":
                            print(value["diff"])
                            return 0
                    elif record.get("applied_at"):
                        if record.get("processed_at"):
                            raise TaskError("task was already applied and acknowledged")
                        # Acknowledgment retries must not reapply edits.
                        acknowledge(record, directory)
                        value = summary(record)
                    else:
                        value = retrieve(record)
                        if value["status"] != "completed":
                            raise TaskError("only successful tasks can be applied")
                        project = Path(record["project"])
                        with project_locked(project):
                            apply_changes(
                                project,
                                value["changes"],
                                directory / "application-backup.json",
                            )
                            # Preserve the application even if acknowledgment
                            # fails or the process exits before it completes.
                            record.update(applied_at=time.time(), status="applied")
                            save(directory / "task.json", record)
                        acknowledge(record, directory)
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
    submit_parser.add_argument("--account", default="agent")
    submit_parser.add_argument(
        "--selector", choices=sorted(OMP_WRITER_SELECTORS), default=DEFAULT_SELECTOR
    )
    submit_parser.add_argument("--timeout", type=float, default=1800)
    submit_parser.add_argument("--check", help="validation argv, shell-quoted; no shell expansion")
    submit_parser.add_argument("--id", help="caller-generated idempotency identity")
    submit_parser.set_defaults(handler=handler)
    selectors_parser = actions.add_parser(
        "selectors", help="list admitted writers; submit probes target-host readiness"
    )
    selectors_parser.add_argument("--json", action="store_true")
    selectors_parser.set_defaults(handler=handler)
    actions.add_parser("list").set_defaults(handler=handler)
    for action in ("status", "resume", "cancel", "retrieve", "diff", "apply"):
        item = actions.add_parser(action)
        item.add_argument("id")
        item.set_defaults(handler=handler)
