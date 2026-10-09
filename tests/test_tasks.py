from __future__ import annotations

import json
import shlex
from itertools import pairwise
from pathlib import Path

import pytest
from conftest import PINNED_BIN, PINNED_BUILD, REAL_ENSURE_WORKER

from agent_offload import tasks, worker_build
from agent_offload.task_protocol import (
    TaskError,
    digest,
    encode,
    manifest,
    render_diff,
    safe_path,
    validate_changes,
)
from agent_offload.tasks import apply_changes, locked, read_record, submit, task_title


@pytest.mark.parametrize(
    "brief,expected",
    [
        ("Fix hello\nMore detail", "Fix hello"),
        ("# Task: Fix hello", "Fix hello"),
        ("## task:   Fix hello", "Fix hello"),
        ("  \n\n  # Task: Fix hello  \nDetails", "Fix hello"),
        ("a" * 161, "a" * 160),
        ("#", "#"),
        ("  \n\t ", "Untitled task"),
        ("# Task:  ", "# Task:"),
    ],
)
def test_task_title(brief, expected):
    assert task_title(brief) == expected


def test_worker_census_failure_is_retrievable(tmp_path, monkeypatch):
    import sys

    from agent_execution.identity import source_sha256

    from agent_offload.protocol import package_source_sha256
    from agent_offload.task_protocol import SCHEMA, digest, result_path
    from agent_offload.task_worker import main
    from agent_offload.tasks import retrieve

    project = tmp_path / "project"
    project.mkdir()
    request, _ = proposal(project)
    request.update(
        schema_version=SCHEMA,
        id="census-failure",
        brief="Edit hello.py",
        selector="openai-codex/gpt-6-luna",
        check=[],
        timeout=30,
        execution_sha256=source_sha256(),
        offload_sha256=package_source_sha256(),
    )
    # This unsupported snapshot entry is outside the submitted baseline.
    (project / "unexpected-link").symlink_to(project / "hello.py")
    payload = tmp_path / "payload"
    payload.mkdir()
    data = json.dumps(request).encode()
    (payload / "coding-task").write_bytes(data)
    monkeypatch.setenv("WEFT_PAYLOAD_DIR", str(payload))
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        sys, "argv", ["worker", "--request-sha256", digest(data), "--task-id", request["id"]]
    )
    with pytest.raises(SystemExit) as failure:
        main()
    assert failure.value.code == 1
    artifact = project / result_path(request["id"])
    result = json.loads(artifact.read_text())
    assert result["status"] == "failed"
    assert "symlink" in result["report"]
    assert result["worker"] is None
    tools = tmp_path / "bin"
    tools.mkdir()
    executable = tools / "weft"
    executable.write_text(f'#!/bin/sh\n/bin/cat "{artifact}"\n')
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    record = {
        "id": request["id"],
        "request": request,
        "request_sha256": digest(data),
        "job_id": "wj123",
    }
    assert retrieve(record)["report"] == result["report"]
    result["status"] = "completed"
    artifact.write_text(json.dumps(result))
    with pytest.raises(TaskError, match="invalid task worker failure"):
        retrieve(record)


def proposal(root: Path) -> tuple[dict, list[dict]]:
    (root / "hello.py").write_text("print('before')\n")
    (root / "hello.py").chmod(0o644)
    request = {"baseline": manifest(root, ["hello.py"]), "carried": {}}
    changes = [
        {
            "path": "hello.py",
            "before": encode(b"print('before')\n"),
            "after": encode(b"print('after')\n"),
            "before_mode": 0o644,
            "after_mode": 0o644,
        }
    ]
    return request, changes


def test_apply_validated_changes_and_retain_backup(tmp_path):
    request, changes = proposal(tmp_path)
    validate_changes(request, changes)
    assert "-print('before')" in render_diff(changes)
    backup = tmp_path / "backup.json"
    apply_changes(tmp_path, changes, backup)
    assert (tmp_path / "hello.py").read_text() == "print('after')\n"
    assert json.loads(backup.read_text())["changes"] == changes


def test_preflight_all_files_before_any_write(tmp_path):
    request, changes = proposal(tmp_path)
    changes.append(
        {
            "path": "new.py",
            "before": None,
            "after": encode(b"new"),
            "before_mode": None,
            "after_mode": 0o644,
        }
    )
    validate_changes(request, changes)
    (tmp_path / "new.py").write_text("another author")
    with pytest.raises(TaskError, match="changed since submission"):
        apply_changes(tmp_path, changes, tmp_path / "backup.json")
    assert (tmp_path / "hello.py").read_text() == "print('before')\n"
    assert not (tmp_path / "backup.json").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../outside",
        "/tmp/outside",
        ".agent-execution/x",
        ".GIT/config",
        "a/../../x",
        "a\\b",
        ".codex/config",
    ],
)
def test_paths_cannot_escape_or_change_metadata(tmp_path, name):
    with pytest.raises(TaskError):
        safe_path(tmp_path, name)


def test_apply_refuses_symlink_parent(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (tmp_path / "link").symlink_to(other, target_is_directory=True)
    with pytest.raises(TaskError, match="symlink"):
        safe_path(tmp_path, "link/new.py")


def test_untrusted_change_must_match_retained_baseline(tmp_path):
    request, changes = proposal(tmp_path)
    changes[0]["before"] = encode(b"invented baseline")
    with pytest.raises(TaskError, match="baseline bytes"):
        validate_changes(request, changes)


def test_duplicate_paths_and_binary_changes_are_rejected(tmp_path):
    request, changes = proposal(tmp_path)
    with pytest.raises(TaskError, match="duplicate"):
        validate_changes(request, changes * 2)
    changes[0]["after"] = encode(b"\xff")
    with pytest.raises(TaskError, match="UTF-8"):
        validate_changes(request, changes)


def test_task_lock_excludes_concurrent_mutations(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path))
    with locked("task1"), pytest.raises(TaskError, match="another operation"), locked("task1"):
        pytest.fail("lock admitted a second owner")


def test_case_insensitive_change_collisions_are_rejected(tmp_path):
    changes = [
        {
            "path": name,
            "before": None,
            "after": encode(b"new"),
            "before_mode": None,
            "after_mode": 0o644,
        }
        for name in ("New.py", "new.py")
    ]
    with pytest.raises(TaskError, match="duplicate"):
        validate_changes({"baseline": {}}, changes)


def test_applied_state_survives_unavailable_job_service(monkeypatch):
    from agent_offload.tasks import status

    monkeypatch.setenv("PATH", "/no-executables")
    row = status({"id": "applied", "status": "applied", "job_id": "wj42", "applied_at": 123})
    assert row["status"] == "applied"
    assert row["applied_at"] == 123
    assert row["error"] is None


def test_corrupt_task_is_visible_without_hiding_healthy_tasks(tmp_path, monkeypatch, capsys):
    import argparse

    from agent_offload.tasks import handler, save

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path))
    with locked("broken") as directory:
        (directory / "task.json").write_text("not JSON")
    with locked("healthy") as directory:
        save(
            directory / "task.json",
            {
                "schema_version": "agent-offload.task/v1",
                "id": "healthy",
                "project": "/project",
                "host": "studio",
                "title": "Ready",
                "status": "submitting",
                "job_id": None,
            },
        )
    assert handler(argparse.Namespace(action="list")) == 0
    rows = json.loads(capsys.readouterr().out)["tasks"]
    assert {row["id"] for row in rows} == {"healthy", "broken"}
    assert next(row for row in rows if row["id"] == "broken")["error"]


def fake_tools(tmp_path, monkeypatch, *, files: list[str], excludes: list[str], run: str) -> None:
    """Put fake rg and weft executables alone on PATH.

    rg lists files; `weft sync inspect` reports excludes; any other weft
    command runs the shell fragment run.
    """
    tools = tmp_path / "bin"
    tools.mkdir()
    listing = tmp_path / "rg-files"
    listing.write_text("".join(f"{name}\n" for name in files))
    inspection = tmp_path / "inspection.json"
    inspection.write_text(json.dumps({"local_dir": "unused", "excludes": excludes}))
    for name, content in {
        "rg": f'#!/bin/sh\n/bin/cat "{listing}"\n',
        "weft": (
            f'#!/bin/sh\nif [ "$1 $2" = "sync inspect" ]; then /bin/cat "{inspection}"; exit 0; fi\n'
            f"{run}\n"
        ),
    }.items():
        path = tools / name
        path.write_text(content)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))


def test_baseline_omits_files_weft_snapshot_excludes(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    project = tmp_path / "project"
    files = [
        "hello.py",
        "CLAUDE.md",
        "testdata/case.txt",
        "src/bin/tool.sh",
        "src/module.pyc",
        "src/keep.py",
        "notes/draft.txt",
    ]
    for name in files:
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(name)
    fake_tools(
        tmp_path,
        monkeypatch,
        files=files,
        excludes=["CLAUDE.md", "testdata", "bin", "*.pyc", "[^a-m]otes"],
        run="exit 1",
    )
    with pytest.raises(TaskError, match="resume this identity"):
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity="snapshot",
        )
    assert sorted(read_record("snapshot")["request"]["baseline"]) == ["hello.py", "src/keep.py"]


@pytest.mark.parametrize("pattern", ["/harness/src", "/harnes?/s[^a-q]c"])
def test_snapshot_anchored_exclusions_preserve_other_paths(tmp_path, monkeypatch, pattern):
    project = tmp_path / "project"
    files = [
        "harness/src/drop.py",
        "harness/src/deep/drop.py",
        "harness/src-extra/keep.py",
        "harness/deep/src/keep.py",
        "nested/harness/src/keep.py",
        "src/keep.py",
    ]
    for name in files:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    fake_tools(tmp_path, monkeypatch, files=files, excludes=[pattern], run="exit 1")
    assert tasks.snapshot_names(project) == files[2:]


@pytest.mark.parametrize("pattern", ["/harness/src", "/harness"])
def test_snapshot_anchored_exclusion_skips_link_but_not_nested_namesake(
    tmp_path, monkeypatch, pattern
):
    project = tmp_path / "project"
    (project / "harness").mkdir(parents=True)
    (project / "hello.py").write_text("hello")
    (project / "harness" / "src").symlink_to("../hello.py")
    fake_tools(tmp_path, monkeypatch, files=["hello.py"], excludes=[pattern], run="exit 1")
    assert tasks.snapshot_names(project) == ["hello.py"]

    (project / "nested" / "harness").mkdir(parents=True)
    (project / "nested" / "harness" / "src").symlink_to("../../hello.py")
    with pytest.raises(TaskError, match=r"carries \(nested/harness/src\)"):
        tasks.snapshot_names(project)


@pytest.mark.parametrize(
    "pattern", ["/shared/nested", "/shared/nested/big.bin", "/shared/*/big.bin"]
)
def test_snapshot_anchored_exclusion_filters_dereferenced_content(tmp_path, monkeypatch, pattern):
    from agent_offload.task_protocol import MAX_FILE

    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.py").write_text("hello")
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    big_file = outside / "nested" / "big.bin"
    with big_file.open("wb") as stream:
        stream.truncate(MAX_FILE + 1)
    (project / "shared").symlink_to(outside)
    fake_tools(tmp_path, monkeypatch, files=["hello.py"], excludes=[pattern], run="exit 1")
    assert tasks.snapshot_names(project) == ["hello.py"]

    # The same content at a different snapshot path is still checked.
    (project / "other").symlink_to(outside)
    with pytest.raises(TaskError, match=r"\(other/nested/big.bin\)"):
        tasks.snapshot_names(project)


def receipt(key: str, decision: str) -> dict:
    return {
        "api_version": "weft.run.receipt.v1",
        "job_id": "wj77",
        "placement_decision": decision,
        "selected_host": "studio",
        "source_pin": "pin",
        "accepted_immediately": False,
        "deduplicated": decision == "deduplicated",
        "idempotency_key": key,
    }


CANCELLED_JOB = '{"id": "%s", "status": "cancelled"}'


def fence_answer(fenced, key="slow", **extra) -> str:
    """A contract-shaped fence answer; `extra` adds contract violations."""
    answer = {"api_version": "weft.job.fence.v1", "idempotency_key": key, "fenced": fenced}
    return json.dumps(answer | extra)


def fake_weft_submission(
    tmp_path, monkeypatch, *, runs: list[str], lookup: str, fence: str = "answer"
) -> Path:
    """Fake weft whose successive `weft run` calls follow `runs`, logging every call.

    Each run is "hang" (outlive the submit budget), "queued" (print a receipt
    for a job left to the daemon, as --no-sync does), "unresolved" (fail after
    Weft recorded the job, so the receipt is lost), or "interrupted" (interrupt
    the submitting process with SIGINT while the command is still running, so
    nothing after it persists). `lookup` is "found",
    "absent", "other-key" (an answer about a different key), or "fail" for
    `weft job lookup`. `fence` is "answer" (serve fence.json, refusing when it
    is absent), "unavailable", or "hang" for `weft job fence`, which records
    its working directory. job.json answers `job inspect`, and `weft cancel`
    rewrites it to the cancelled state; a refuse-cancel or refuse-mark file
    fails that command.
    """
    monkeypatch.setattr(tasks, "SUBMIT_TIMEOUT", 1)
    calls = tmp_path / "weft-calls"
    queued = tmp_path / "queued.json"
    queued.write_text(json.dumps(receipt("slow", "queued")))
    answers = {
        "found": {"found": True, "receipt": receipt("slow", "deduplicated")},
        "absent": {"found": False},
    }
    answer = tmp_path / "lookup.json"
    if lookup in answers:
        answer.write_text(
            json.dumps(
                {"api_version": "weft.job.lookup.v1", "idempotency_key": "slow", **answers[lookup]}
            )
        )
    elif lookup == "other-key":
        answer.write_text(
            json.dumps(
                {"api_version": "weft.job.lookup.v1", "idempotency_key": "x", "found": False}
            )
        )
    fence_file = tmp_path / "fence.json"
    fence_cwd = tmp_path / "fence-cwd"
    job_state = tmp_path / "job.json"
    fence_steps = {
        "answer": (
            f'if [ -f "{fence_file}" ]; then /bin/cat "{fence_file}"; '
            'else echo "fence unavailable" >&2; exit 1; fi'
        ),
        "unavailable": 'echo "fence unavailable" >&2; exit 1',
        "hang": "exec /bin/sleep 5",
    }
    fence_step = fence_steps[fence]
    lookup_step = (
        "echo 'ledger unreadable' >&2; exit 1" if lookup == "fail" else f'/bin/cat "{answer}"'
    )
    steps = {
        "hang": "exec /bin/sleep 5",
        "queued": f"/bin/cat {queued}",
        "unresolved": "echo 'transport lost after acceptance' >&2; exit 1",
        "interrupted": 'kill -INT "$PPID"; exec /bin/sleep 5',
    }
    run_steps = "".join(
        f"  {index}) {steps[step]} ;;\n" for index, step in enumerate(runs, start=1)
    )
    fake_tools(
        tmp_path,
        monkeypatch,
        files=["hello.py"],
        excludes=[".git"],
        run=(
            f'echo "$*" >> "{calls}"\n'
            f'if [ "$1 $2 $3" = "job fence --idempotency-key" ]; then pwd > "{fence_cwd}"; {fence_step}; exit 0; fi\n'
            f'if [ "$1 $2" = "job inspect" ]; then /bin/cat "{job_state}"; exit 0; fi\n'
            f'if [ "$1" = "cancel" ]; then if [ -f "{tmp_path}/refuse-cancel" ]; then /bin/cp "{tmp_path}/state/tasks/slow/task.json" "{tmp_path}/record-at-cancel.json"; echo "weft: the daemon cannot be reached" >&2; exit 1; fi; printf {shlex.quote(CANCELLED_JOB)} "$2" > "{job_state}"; exit 0; fi\n'
            f'if [ "$1 $2" = "job mark-processed" ]; then if [ -f "{tmp_path}/refuse-mark" ]; then /bin/cp "{tmp_path}/state/tasks/slow/task.json" "{tmp_path}/record-at-mark.json"; echo "weft: the ledger refuses the processed transition" >&2; exit 1; fi; exit 0; fi\n'
            f'if [ "$1 $2" = "job lookup" ]; then {lookup_step}; exit 0; fi\n'
            f'case "$(/usr/bin/grep -c "^run " "{calls}")" in\n{run_steps}esac'
        ),
    )
    return calls


def submit_slow(tmp_path: Path) -> dict:
    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.py").write_text("hello")
    return submit(
        project,
        "Fix hello",
        host="studio",
        selector="openai-codex/gpt-6-astra",
        timeout=60,
        check=[],
        identity="slow",
    )


def weft_calls(calls: Path) -> list[str]:
    return [line.split()[0] + " " + line.split()[1] for line in calls.read_text().splitlines()]


def test_submit_leaves_host_dispatch_to_weft_and_binds_the_queued_job(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["queued"], lookup="fail")

    submit_slow(tmp_path)

    record = read_record("slow")
    assert (record["status"], record["job_id"]) == ("queued", "wj77")
    assert "--no-sync" in calls.read_text().split()


def test_submit_binds_the_job_a_timed_out_weft_run_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["hang"], lookup="found")

    submit_slow(tmp_path)

    record = read_record("slow")
    assert (record["status"], record["job_id"]) == ("queued", "wj77")
    assert weft_calls(calls) == ["run -C", "job lookup"]


def test_submit_resubmits_once_when_a_timed_out_weft_run_recorded_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["hang", "queued"], lookup="absent")

    submit_slow(tmp_path)

    record = read_record("slow")
    assert (record["status"], record["job_id"]) == ("queued", "wj77")
    assert weft_calls(calls) == ["run -C", "job lookup", "run -C"]


@pytest.mark.parametrize("lookup", ["fail", "other-key"])
def test_submit_is_unresolved_when_the_lookup_after_a_timeout_is_not_an_answer(
    tmp_path, monkeypatch, lookup
):
    # Neither an unreadable ledger nor an answer about another key is evidence
    # that the job is absent, so nothing is resubmitted.
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["hang", "queued"], lookup=lookup)

    with pytest.raises(TaskError, match="resume this identity"):
        submit_slow(tmp_path)

    assert read_record("slow")["status"] == "submission_unknown"
    assert weft_calls(calls) == ["run -C", "job lookup"]


def test_baseline_requires_weft_source_exclusions(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.py").write_text("hello")
    fake_tools(tmp_path, monkeypatch, files=["hello.py"], excludes=[], run="exit 1")
    (tmp_path / "inspection.json").write_text(json.dumps({"local_dir": "unused"}))
    with pytest.raises(TaskError, match="source exclusions"):
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity="uninspected",
        )


def test_submission_uses_repository_project_and_clean_title(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.py").write_text("hello")
    arguments = tmp_path / "weft-argv"
    fake_tools(
        tmp_path,
        monkeypatch,
        files=["hello.py"],
        excludes=[".git"],
        run=f"printf '%s\\n' \"$@\" > {shlex.quote(str(arguments))}\nexit 1",
    )
    with pytest.raises(TaskError, match="resume this identity"):
        submit(
            project,
            "\n# Task: Fix hello\nUpdate hello.py",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity="readable",
        )
    argv = arguments.read_text().splitlines()
    assert argv[0] == "run"
    assert "--project" not in argv
    pairs = list(pairwise(argv))
    assert ("--tag", "inventory") in pairs
    assert ("--tag", "agent-offload-task") in pairs
    assert argv[argv.index("-m") + 1] == "Fix hello"
    assert read_record("readable")["title"] == "Fix hello"


def test_submission_timeout_retains_identity_and_request(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.py").write_text("hello")
    # External executables are simulated; the task's submission and persistence
    # code run unchanged, including the failure path after payload publication.
    fake_tools(
        tmp_path,
        monkeypatch,
        files=["hello.py"],
        excludes=[".git"],
        run="echo 'transport lost after acceptance' >&2\nexit 1",
    )
    with pytest.raises(TaskError, match="resume this identity"):
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity="retained",
        )
    record = read_record("retained")
    assert record["status"] == "submission_unknown"
    assert record["request"]["baseline"]["hello.py"]
    with pytest.raises(TaskError, match="already exists"):
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity="retained",
        )


@pytest.mark.parametrize(
    "check_source,refused",
    [
        ("Path('helper.py').write_text('value = 42\\n')", True),
        ("Path('generated.py').write_text('value = 42\\n')", True),
        ("Path('helper.py').unlink()", True),
        ("Path('hello.py').chmod(0o755)", True),
        (
            "Path('__pycache__').mkdir(); Path('__pycache__/helper.pyc').write_bytes(b'cache')",
            False,
        ),
    ],
    ids=["edit-unproposed", "create", "delete-unproposed", "chmod", "ignored-cache"],
)
def test_check_must_preserve_complete_proposal(tmp_path, monkeypatch, check_source, refused):
    import sys
    from types import SimpleNamespace

    from agent_offload import task_worker

    request, changes = proposal(tmp_path)
    (tmp_path / "helper.py").write_text("value = 0\n")
    request.update(
        id="check-source",
        brief="Edit hello.py",
        selector="openai-codex/gpt-6-astra",
        timeout=30,
        execution_sha256="unused-by-simulated-writer",
        request_sha256="unused-by-simulated-writer",
        baseline=manifest(tmp_path, ["hello.py", "helper.py"]),
        check=[sys.executable, "-c", "from pathlib import Path; " + check_source],
    )

    # Simulate the external LLM only; validation runs as a real subprocess.
    def write_proposal(**kwargs):
        (tmp_path / "hello.py").write_text("print('after')\n")
        return SimpleNamespace(status="completed", harness=SimpleNamespace(stdout=""), to_dict=dict)

    monkeypatch.setattr(task_worker, "execute_worker", write_proposal)
    monkeypatch.setattr(task_worker, "omp_final_text", lambda output: "Edited hello.py")
    result = task_worker.execute(request, tmp_path)
    assert result["changes"] == changes
    if refused:
        # The check exited 0, yet the result is a retrievable validation
        # failure that keeps the writer's evidence and can never be applied.
        assert result["status"] == "validation_failed"
        assert result["validation"]["exit_code"] == 0
        assert result["validation"]["error"] == "validation modified proposed source"
    else:
        assert result["status"] == "completed"


def test_a_file_over_the_limit_is_carried_instead_of_refusing_the_project(tmp_path, monkeypatch):
    # A repository holding a corpus or fixture still uses the task lane: the
    # large file leaves the editable baseline rather than the snapshot.
    from agent_offload import task_protocol

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(task_protocol, "MAX_FILE", 16)
    project = tmp_path / "project"
    files = ["hello.py", "data/corpus.jsonl"]
    for name, text in zip(files, ["print('hi')\n", "a" * 64]):
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text)
        (project / name).chmod(0o644)
    fake_tools(tmp_path, monkeypatch, files=files, excludes=[], run="exit 1")
    with pytest.raises(TaskError, match="resume this identity"):
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity="carried",
        )
    request = read_record("carried")["request"]
    assert list(request["baseline"]) == ["hello.py"]
    assert request["carried"] == {"data/corpus.jsonl": {"size": 64, "mode": 0o644}}


def test_a_carried_file_is_readable_but_never_an_edit(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from agent_offload import task_protocol, task_worker

    monkeypatch.setattr(task_protocol, "MAX_FILE", 16)
    request, changes = proposal(tmp_path)
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text("a" * 64)
    corpus.chmod(0o644)
    request.update(
        id="carried-worker",
        brief="Edit hello.py",
        selector="openai-codex/gpt-6-astra",
        timeout=30,
        execution_sha256="unused-by-simulated-writer",
        request_sha256="unused-by-simulated-writer",
        carried={"corpus.jsonl": {"size": 64, "mode": 0o644}},
        # The check reads the carried file the snapshot delivered.
        check=[sys.executable, "-c", "assert len(open('corpus.jsonl').read()) == 64"],
    )

    def write_proposal(**kwargs):
        (tmp_path / "hello.py").write_text("print('after')\n")
        return SimpleNamespace(status="completed", harness=SimpleNamespace(stdout=""), to_dict=dict)

    monkeypatch.setattr(task_worker, "execute_worker", write_proposal)
    monkeypatch.setattr(task_worker, "omp_final_text", lambda output: "Edited hello.py")
    result = task_worker.execute(request, tmp_path)
    assert (result["status"], result["changes"]) == ("completed", changes)

    (tmp_path / "hello.py").write_text("print('before')\n")

    def rewrite_corpus(**kwargs):
        corpus.write_text("b" * 65)
        return SimpleNamespace(status="completed", harness=SimpleNamespace(stdout=""), to_dict=dict)

    monkeypatch.setattr(task_worker, "execute_worker", rewrite_corpus)
    with pytest.raises(TaskError, match="over the task file limit: corpus.jsonl"):
        task_worker.execute(request, tmp_path)


def test_a_returned_edit_of_a_carried_file_is_refused(tmp_path):
    request, changes = proposal(tmp_path)
    request["carried"] = {"hello.py": {"size": 4096, "mode": 0o644}}
    with pytest.raises(TaskError, match="over the task file limit: hello.py"):
        validate_changes(request, changes)


def test_resume_still_checks_a_retained_request_that_predates_carried_files(tmp_path):
    # An unresolved submission retained before carried files existed resumes
    # under its own idempotency identity; its baseline check is unchanged.
    from agent_offload.task_protocol import census
    from agent_offload.tasks import check_baseline_unchanged

    (tmp_path / "hello.py").write_text("print('before')\n")
    (tmp_path / "hello.py").chmod(0o644)
    record = {"project": str(tmp_path), "request": {"baseline": manifest(tmp_path, ["hello.py"])}}
    check_baseline_unchanged(record)
    (tmp_path / "hello.py").write_text("print('changed')\n")
    with pytest.raises(TaskError, match="project changed since submission"):
        check_baseline_unchanged(record)

    # A request that does carry one compares it too.
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text("a" * 64)
    record["request"]["baseline"] = manifest(tmp_path, ["hello.py"])
    record["request"]["carried"] = census(tmp_path, ["corpus.jsonl"])
    check_baseline_unchanged(record)
    corpus.write_text("a" * 65)
    with pytest.raises(TaskError, match="project changed since submission"):
        check_baseline_unchanged(record)


def test_selectors_report_the_accepted_writers(capsys):
    from agent_execution.omp_execution import OMP_WRITER_SELECTORS

    from agent_offload import cli
    from agent_offload.task_protocol import SELECTORS_SCHEMA

    with pytest.raises(SystemExit) as exited:
        cli.main(["task", "selectors", "--json"])
    assert exited.value.code == 0
    value = json.loads(capsys.readouterr().out)
    assert value["schema_version"] == SELECTORS_SCHEMA
    assert value["selectors"] == sorted(OMP_WRITER_SELECTORS)
    assert value["default"] in value["selectors"]

    with pytest.raises(SystemExit) as exited:
        cli.main(["task", "selectors"])
    assert exited.value.code == 0
    assert capsys.readouterr().out.split() == sorted(OMP_WRITER_SELECTORS)


def test_submit_accepts_exactly_the_listed_selectors():
    from agent_offload import cli
    from agent_offload.tasks import selectors

    parser = cli.build_parser()
    for selector in selectors()["selectors"]:
        args = parser.parse_args(["task", "submit", ".", "--prompt", "x", "--selector", selector])
        assert args.selector == selector
    with pytest.raises(SystemExit):
        parser.parse_args(["task", "submit", ".", "--prompt", "x", "--selector", "none/none"])


def test_refused_selector_and_refused_timeout_say_which_one_failed(tmp_path):
    from agent_execution.omp_execution import OMP_WRITER_SELECTORS

    admitted = min(OMP_WRITER_SELECTORS)
    with pytest.raises(TaskError) as refusal:
        submit(tmp_path, "brief", host="studio", selector="none/none", timeout=60, check=[])
    assert "none/none" in str(refusal.value)
    assert "timeout" not in str(refusal.value)

    with pytest.raises(TaskError) as refusal:
        submit(tmp_path, "brief", host="studio", selector=admitted, timeout=99999, check=[])
    assert "timeout" in str(refusal.value)
    assert "selector" not in str(refusal.value)


def identity_stub(tmp_path, source_sha256: str) -> None:
    """Add an agent-execution-worker identity probe beside the fake rg/weft tools."""
    import shlex

    payload = json.dumps(
        {"schema_version": "agent-execution.worker-identity/v1", "source_sha256": source_sha256}
    )
    worker = tmp_path / "bin" / "agent-execution-worker"
    worker.write_text(f"#!/bin/sh\n/bin/echo {shlex.quote(payload)}\n")
    worker.chmod(0o755)


def bare_project(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.py").write_text("hello")
    return project


def test_submit_uses_pinned_build_despite_unrelated_path_worker(tmp_path, monkeypatch):
    from agent_execution.identity import source_sha256

    project = bare_project(tmp_path, monkeypatch)
    accepted = json.dumps(receipt("isolated", "queued"))
    fake_tools(
        tmp_path,
        monkeypatch,
        files=["hello.py"],
        excludes=[],
        run=f"/bin/echo {shlex.quote(accepted)}",
    )
    identity_stub(tmp_path, "0" * 64)
    submit(
        project,
        "Fix hello",
        host="studio",
        selector="openai-codex/gpt-6-astra",
        timeout=60,
        check=[],
        identity="isolated",
    )
    record = read_record("isolated")
    assert record["job_id"] == "wj77"
    assert record["request"]["execution_sha256"] == source_sha256()
    assert record["bin_dir"] == PINNED_BIN


def test_submit_refuses_swiftpm_path_dependencies_outside_the_project(tmp_path, monkeypatch):
    project = bare_project(tmp_path, monkeypatch)
    (project / "Package.swift").write_text(
        "// swift-tools-version:5.9\n"
        "dependencies = [\n"
        '  .package(path: "../workflow-state"),\n'
        '  // .package(path: "../commented-out"),\n'
        '  .package(url: "https://github.com/apple/swift-argument-parser", from: "1.0.0"),\n'
        "]\n"
    )
    with pytest.raises(TaskError) as refusal:
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=["just", "check"],
            identity="swift",
        )
    assert "../workflow-state" in str(refusal.value)
    assert "commented-out" not in str(refusal.value)
    assert "remote workspace that also holds those dependencies" in str(refusal.value)
    # Without a check nothing builds remotely, so the manifest is accepted.
    fake_tools(tmp_path, monkeypatch, files=["hello.py"], excludes=[], run="exit 1")
    with pytest.raises(TaskError, match="resume this identity"):
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity="swift-no-check",
        )


def test_submit_allows_path_dependencies_inside_the_project(tmp_path, monkeypatch):
    project = bare_project(tmp_path, monkeypatch)
    (project / "Package.swift").write_text(
        'dependencies = [\n  .package(path: "Packages/Helper"),\n]\n'
    )
    (project / "Packages" / "Helper").mkdir(parents=True)
    fake_tools(tmp_path, monkeypatch, files=["hello.py"], excludes=[], run="exit 1")
    # The weft stub refuses every run, so reaching it proves the manifest passed.
    with pytest.raises(TaskError, match="resume this identity"):
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity="contained",
        )


def test_submit_refuses_pyproject_path_sources_outside_the_project(tmp_path, monkeypatch):
    project = bare_project(tmp_path, monkeypatch)
    (project / "pyproject.toml").write_text(
        "[project]\n"
        'name = "sample"\n'
        'dependencies = ["sample-lib @ file:///opt/outside"]\n'
        "\n"
        "[tool.uv.sources]\n"
        'sample-lib = { path = "../sample-lib" }\n'
    )
    with pytest.raises(TaskError) as refusal:
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=["just", "check"],
            identity="python",
        )
    assert "../sample-lib" in str(refusal.value)
    assert "/opt/outside" in str(refusal.value)


def test_submit_ignores_commented_and_placeholder_pyproject_paths(tmp_path, monkeypatch):
    project = bare_project(tmp_path, monkeypatch)
    (project / "pyproject.toml").write_text(
        "[project]\n"
        'name = "sample"\n'
        '# dependencies = ["gone @ file:///opt/commented"]\n'
        'dependencies = ["local @ file:///${PROJECT_ROOT}/packages/local"]\n'
        "\n"
        "[tool.uv.sources]\n"
        '# local = { path = "../commented" }\n'
        'local = { path = "packages/local" }\n'
    )
    (project / "packages" / "local").mkdir(parents=True)
    fake_tools(tmp_path, monkeypatch, files=["hello.py"], excludes=[], run="exit 1")
    # The weft stub refuses every run, so reaching it proves no path was refused.
    with pytest.raises(TaskError, match="resume this identity"):
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=["just", "check"],
            identity="placeholders",
        )


def test_submit_refuses_in_root_symlinks_the_snapshot_carries(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "hello.py").write_text("hello")
    (project / "node_modules").mkdir()
    # Weft drops excluded links and snapshots out-of-root links as content.
    (project / "node_modules" / "tool").symlink_to("../hello.py")
    (project / "cache.lnk").symlink_to("hello.py")
    (project / "outside").symlink_to(tmp_path)
    (project / "escape.py").symlink_to("../elsewhere.py")
    fake_tools(tmp_path, monkeypatch, files=["hello.py"], excludes=["*.lnk"], run="exit 1")

    def attempt(identity: str) -> None:
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity=identity,
        )

    # Every link above is one the snapshot does not carry, so submission proceeds.
    with pytest.raises(TaskError, match="resume this identity"):
        attempt("unlinked")
    (project / "alias.py").symlink_to("hello.py")
    (project / "src" / "up").symlink_to("..")
    with pytest.raises(TaskError, match=r"carries \(alias.py, src/up\)") as refused:
        attempt("linked")
    assert "escape.py" not in str(refused.value)
    assert not (tmp_path / "state" / "tasks" / "linked" / "task.json").exists()


def test_submit_refuses_oversized_content_behind_out_of_root_links(tmp_path, monkeypatch):
    from agent_offload.task_protocol import MAX_FILE

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.py").write_text("hello")
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "small.txt").write_text("small")
    (outside / "nested" / "cache").mkdir()
    (outside / "nested" / "cache" / "huge.bin").write_bytes(b"x" * (MAX_FILE + 1))
    (project / "shared").symlink_to(outside)
    fake_tools(tmp_path, monkeypatch, files=["hello.py"], excludes=["cache"], run="exit 1")

    def attempt(identity: str) -> None:
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity=identity,
        )

    # The oversized file sits under an excluded directory, so Weft drops it.
    with pytest.raises(TaskError, match="resume this identity"):
        attempt("excluded")
    (outside / "nested" / "big.bin").write_bytes(b"x" * (MAX_FILE + 1))
    (project / "data.bin").symlink_to(outside / "nested" / "big.bin")
    with pytest.raises(TaskError, match=r"data\.bin, shared/nested/big\.bin") as refused:
        attempt("oversized")
    assert "2 MiB" in str(refused.value)
    assert not (tmp_path / "state" / "tasks" / "oversized" / "task.json").exists()


def test_decode_refuses_non_base64_and_oversized_content():
    from agent_offload.task_protocol import MAX_FILE, decode

    with pytest.raises(TaskError, match="base64"):
        decode("not base64!")
    with pytest.raises(TaskError, match="base64"):
        decode("aGVsbG8=\n")
    assert decode(encode(b"x" * MAX_FILE)) == b"x" * MAX_FILE
    with pytest.raises(TaskError, match="file size limit"):
        decode(encode(b"x" * (MAX_FILE + 1)))


def test_change_paths_may_not_be_both_a_file_and_a_directory(tmp_path):
    request, changes = proposal(tmp_path)
    for first, second in (("hello.py", "hello.py/inner"), ("hello.py/inner", "hello.py")):
        nested = {**changes[0], "path": first}
        created = {"path": second, "before": None, "after": encode(b"x"), "before_mode": None}
        created["after_mode"] = 0o644
        with pytest.raises(TaskError):
            validate_changes(request, [nested, created])


def test_change_and_manifest_size_bounds_count_every_file(tmp_path, monkeypatch):
    from agent_offload import task_protocol

    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("12345")
    monkeypatch.setattr(task_protocol, "MAX_TOTAL", 10)
    assert len(manifest(tmp_path, ["a.txt", "b.txt"])) == 2
    monkeypatch.setattr(task_protocol, "MAX_TOTAL", 9)
    with pytest.raises(TaskError, match="exceeds"):
        manifest(tmp_path, ["a.txt", "b.txt"])
    created = [
        {
            "path": name,
            "before": None,
            "after": encode(b"12345"),
            "before_mode": None,
            "after_mode": 0o644,
        }
        for name in ("c.txt", "d.txt")
    ]
    monkeypatch.setattr(task_protocol, "MAX_TOTAL", 10)
    assert validate_changes({"baseline": {}}, created) == created
    monkeypatch.setattr(task_protocol, "MAX_TOTAL", 9)
    with pytest.raises(TaskError, match="bundle exceeds"):
        validate_changes({"baseline": {}}, created)


def test_walk_refuses_a_symlinked_directory(tmp_path):
    from agent_offload.task_protocol import walk_files

    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.txt").write_text("a")
    assert walk_files(tmp_path) == ["real/a.txt"]
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    with pytest.raises(TaskError, match="symlink directory"):
        walk_files(tmp_path)


@pytest.mark.parametrize("name", ["", "a//b", "/abs"])
def test_safe_path_refuses_empty_unnormalized_and_absolute_names(tmp_path, name):
    with pytest.raises(TaskError):
        safe_path(tmp_path, name)


def test_manifest_refuses_an_oversized_regular_file(tmp_path):
    from agent_offload.task_protocol import MAX_FILE

    (tmp_path / "ok.txt").write_bytes(b"x" * MAX_FILE)
    (tmp_path / "big.txt").write_bytes(b"x" * (MAX_FILE + 1))
    assert list(manifest(tmp_path, ["ok.txt"])) == ["ok.txt"]
    with pytest.raises(TaskError, match="at most"):
        manifest(tmp_path, ["big.txt"])


@pytest.mark.parametrize("changes", [{"path": "a"}, ["hello.py"], [None]])
def test_validate_changes_refuses_malformed_payloads(tmp_path, changes):
    request, _ = proposal(tmp_path)
    with pytest.raises(TaskError):
        validate_changes(request, changes)


def test_object_json_refuses_a_non_object():
    from agent_offload.task_protocol import SCHEMA, object_json

    with pytest.raises(TaskError):
        object_json(json.dumps([{"schema_version": SCHEMA}]), SCHEMA)


def test_linked_content_check_follows_nested_links_and_single_files(tmp_path, monkeypatch):
    from agent_offload.task_protocol import MAX_FILE

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.py").write_text("hello")
    store = tmp_path / "store"
    store.mkdir()
    (store / "small.txt").write_text("small")
    (store / "big.bin").write_bytes(b"x" * (MAX_FILE + 1))
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "deep").symlink_to(store)
    (project / "notes.txt").symlink_to(store / "small.txt")
    fake_tools(tmp_path, monkeypatch, files=["hello.py"], excludes=[], run="exit 1")

    def attempt(identity: str) -> None:
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity=identity,
        )

    # A small file behind an out-of-project link is accepted.
    with pytest.raises(TaskError, match="resume this identity"):
        attempt("small-link")
    # A large file is found through a link inside the linked directory.
    (project / "shared").symlink_to(shared)
    with pytest.raises(TaskError, match=r"shared/deep/big\.bin"):
        attempt("nested-link")


def peer_record() -> dict:
    from agent_execution.identity import source_sha256

    from agent_offload.protocol import package_source_sha256

    return {
        "host": "studio",
        "account": "osteele",
        "request": {
            "offload_sha256": package_source_sha256(),
            "execution_sha256": source_sha256(),
        },
    }


def host_sync_fixture_environment(tmp_path: Path, monkeypatch) -> Path:
    """A local stand-in task host, plus a poisoned agent-host-sync stub.

    Task provisioning must never delegate to that tool again, so the stub
    fails loudly and leaves a marker if it is ever executed.
    """
    from test_worker_build import fake_host

    for name, value in fake_host(tmp_path).items():
        monkeypatch.setenv(name, value)
    marker = tmp_path / "agent-host-sync-ran"
    stub = tmp_path / "fake-bin" / "agent-host-sync"
    stub.write_text(f"#!/bin/sh\necho ran > {shlex.quote(str(marker))}\nexit 70\n")
    stub.chmod(0o755)
    return marker


@pytest.mark.usefixtures("agent_host_sync_provider")
def test_host_sync_config_provisions_from_the_installed_pair_over_ssh(tmp_path, monkeypatch):
    marker = host_sync_fixture_environment(tmp_path, monkeypatch)
    record = peer_record()

    REAL_ENSURE_WORKER(record)

    build_id = worker_build.ssh_build_id(
        record["request"]["offload_sha256"], record["request"]["execution_sha256"]
    )
    root = tmp_path / "home/.local/share/agent-offload/builds" / build_id
    assert record["build_id"] == build_id
    assert record["bin_dir"] == str(root / "bin")
    assert (root / "BUILD_ID").read_text().strip() == build_id
    assert not marker.exists()


@pytest.mark.usefixtures("agent_host_sync_provider")
@pytest.mark.parametrize("side", ["agent-offload", "agent-execution"])
def test_host_sync_config_still_refuses_a_pin_the_install_cannot_satisfy(
    tmp_path, monkeypatch, side
):
    marker = host_sync_fixture_environment(tmp_path, monkeypatch)
    record = peer_record()
    if side == "agent-offload":
        record["request"]["offload_sha256"] = "0" * 64
    else:
        record["request"]["execution_sha256"] = "0" * 64

    with pytest.raises(worker_build.PinnedBuildUnavailable) as refused:
        REAL_ENSURE_WORKER(record)

    message = str(refused.value)
    assert f"installed {side} changed" in message
    assert "task pinned 000000000000" in message
    assert "submit a new task for the same brief" in message
    # Nothing was built and nothing was delegated: the refusal precedes any
    # host effect, and the record keeps no build to half-trust.
    assert "bin_dir" not in record and "build_id" not in record
    assert not (tmp_path / "home/.local/share/agent-offload").exists()
    assert not marker.exists()


def test_provider_failure_preserves_remote_failure_detail():
    with pytest.raises(TaskError) as failure:
        worker_build.run_provider(
            ["sh", "-c", "echo 'build failed: uv resolution error' >&2; exit 1"],
            timeout=30,
            failure="worker provisioning on agent@box failed",
        )
    assert "build failed: uv resolution error" in str(failure.value)


def test_submit_launches_the_pinned_build_by_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["queued"], lookup="fail")

    submit_slow(tmp_path)

    record = read_record("slow")
    assert (record["bin_dir"], record["build_id"]) == (PINNED_BIN, PINNED_BUILD)
    run = next(line for line in calls.read_text().splitlines() if line.startswith("run "))
    # Only an absolute path into the build store pins the queued job to its
    # build and lets host cleanup see that the build is still needed.
    assert f"{PINNED_BIN}/agent-offload-task-worker" in run
    assert " agent-offload-task-worker " not in f" {run} "


def fake_writer_readiness(tmp_path: Path, monkeypatch):
    """Synthetic shared observations; retain a log to detect duplicate probing."""
    from conftest import status_envelope

    from agent_offload import readiness

    answer = tmp_path / "execution-status.json"
    probes = tmp_path / "status-calls"

    def observe(
        *, model_available: bool = True, credential_available: bool = True, detail: str = "ready"
    ) -> None:
        status = status_envelope(
            ["openai-codex/gpt-6-astra"],
            overrides={
                "capability": {
                    "state": "available" if model_available else "unavailable",
                    "detail": detail,
                },
                "authentication": {
                    "state": "available" if credential_available else "unavailable",
                    "detail": detail,
                },
            },
        )
        answer.write_text(json.dumps(status))

    def probe(**kwargs):
        with probes.open("a") as stream:
            stream.write("probe\n")
        return json.loads(answer.read_text())

    monkeypatch.setattr(readiness, "probe_remote_status", probe)
    return observe, probes


@pytest.mark.parametrize(
    "missing,detail",
    [
        ("model_available", "Unavailable exact OMP model"),
        ("credential_available", "No execution-eligible OMP credentials"),
    ],
    ids=["model", "credentials"],
)
def test_unavailable_writer_never_queues_and_resume_rechecks(
    tmp_path, monkeypatch, capsys, missing, detail
):
    from agent_offload import cli
    from agent_offload.readiness import require_writer

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(tasks, "require_writer", require_writer)
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["queued"], lookup="absent")
    observe, _ = fake_writer_readiness(tmp_path, monkeypatch)
    observe(**{missing: False}, detail=detail)

    with pytest.raises(TaskError, match=detail):
        submit_slow(tmp_path)
    assert not calls.exists()
    assert read_record("slow")["job_id"] is None
    capsys.readouterr()
    with pytest.raises(SystemExit) as refused:
        cli.main(["task", "resume", "slow"])
    assert refused.value.code == 1
    assert not calls.exists()
    diagnostic = capsys.readouterr().err
    assert detail in diagnostic
    assert "agent-offload task resume slow" in diagnostic

    observe()
    with pytest.raises(SystemExit) as resumed:
        cli.main(["task", "resume", "slow"])
    assert resumed.value.code == 0
    assert read_record("slow")["job_id"] == "wj77"
    assert weft_calls(calls) == ["run -C"]


@pytest.mark.parametrize("interruption", ["unresolved", "interrupted"])
def test_resume_recovers_an_accepted_submission_without_readiness_or_resend(
    tmp_path, monkeypatch, interruption
):
    import signal

    from agent_offload import cli
    from agent_offload.readiness import require_writer

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(tasks, "require_writer", require_writer)
    # Weft records the job, but its receipt never reaches the task record:
    # either the transport fails gracefully or the submitter is interrupted
    # while `weft run` is still running.
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=[interruption], lookup="found")
    observe, probes = fake_writer_readiness(tmp_path, monkeypatch)
    observe()
    if interruption == "unresolved":
        with pytest.raises(TaskError):
            submit_slow(tmp_path)
    else:
        # Python only translates SIGINT into KeyboardInterrupt when it owns the
        # handler; a harness may have inherited SIGINT as ignored.
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            with pytest.raises(KeyboardInterrupt):
                submit_slow(tmp_path)
        finally:
            signal.signal(signal.SIGINT, previous)
    assert read_record("slow")["job_id"] is None
    assert len(probes.read_text().splitlines()) == 1

    # Credentials lost after acceptance cannot turn reconciliation into a resend.
    observe(credential_available=False, detail="No execution-eligible OMP credentials")
    monkeypatch.setattr(tasks, "ensure_worker", REAL_ENSURE_WORKER)
    with pytest.raises(SystemExit) as resumed:
        cli.main(["task", "resume", "slow"])
    assert resumed.value.code == 0
    record = read_record("slow")
    assert (record["status"], record["job_id"]) == ("queued", "wj77")
    assert len(probes.read_text().splitlines()) == 1
    assert weft_calls(calls) == ["run -C", "job lookup"]


def test_fresh_worker_refusal_prevents_generation_after_submit_admission(tmp_path, monkeypatch):
    from agent_execution import execution_status
    from agent_execution.identity import source_sha256
    from conftest import status_envelope

    from agent_offload import readiness, task_worker

    request, _ = proposal(tmp_path)
    selector = "openai-codex/gpt-6-luna"
    request.update(
        id="late-refusal",
        selector=selector,
        execution_sha256=source_sha256(),
        brief="Edit hello.py",
        timeout=30,
        check=[],
        request_sha256="a" * 64,
    )
    monkeypatch.setattr(
        readiness,
        "probe_remote_status",
        lambda **kwargs: status_envelope([selector]),
    )
    readiness.require_writer({"host": "studio", "bin_dir": PINNED_BIN, "request": request})
    refused = status_envelope(
        [selector],
        overrides={
            "authentication": {"state": "unavailable", "detail": "credential revoked"},
        },
    )
    monkeypatch.setattr(execution_status, "probe_status", lambda **kwargs: refused)

    def unexpected_generation(**kwargs):
        raise AssertionError("revoked credentials reached the model launch")

    monkeypatch.setattr(task_worker, "execute_worker", unexpected_generation)
    with pytest.raises(TaskError, match="credential revoked"):
        task_worker.execute(request, tmp_path)
    assert (tmp_path / "hello.py").read_text() == "print('before')\n"


def test_predispatch_refusal_can_be_cancelled_without_queuing_or_resurrection(
    tmp_path, monkeypatch, capsys
):
    from agent_offload import cli
    from agent_offload.readiness import require_writer

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(tasks, "require_writer", require_writer)
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["queued"], lookup="absent")
    observe, _ = fake_writer_readiness(tmp_path, monkeypatch)
    observe(model_available=False, detail="Exact model absent")
    with pytest.raises(TaskError):
        submit_slow(tmp_path)
    original = read_record("slow")
    for _ in range(2):
        with pytest.raises(SystemExit) as outcome:
            cli.main(["task", "cancel", "slow"])
        assert outcome.value.code == 0
    cancelled = read_record("slow")
    assert cancelled["status"] == "cancelled"
    assert cancelled["request"] == original["request"]
    assert cancelled["job_id"] is None
    assert not calls.exists()
    observe()
    with pytest.raises(SystemExit) as outcome:
        cli.main(["task", "resume", "slow"])
    assert outcome.value.code == 1
    assert not calls.exists()
    assert read_record("slow")["status"] == "cancelled"


@pytest.mark.parametrize("lookup", ["absent", "fail", "other-key"])
def test_unresolved_submission_keeps_custody_after_lookup_or_admission_refusal(
    tmp_path, monkeypatch, capsys, lookup
):
    from agent_offload import cli
    from agent_offload.readiness import require_writer

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(tasks, "require_writer", require_writer)
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["unresolved"], lookup=lookup)
    observe, _ = fake_writer_readiness(tmp_path, monkeypatch)
    observe()
    with pytest.raises(TaskError):
        submit_slow(tmp_path)
    original = read_record("slow")
    assert original["submission_started"] is True
    observe(credential_available=False, detail="Credential revoked after submission")
    with pytest.raises(SystemExit) as resumed:
        cli.main(["task", "resume", "slow"])
    assert resumed.value.code == 1
    with pytest.raises(SystemExit) as cancelled:
        cli.main(["task", "cancel", "slow"])
    assert cancelled.value.code == 1
    current = read_record("slow")
    assert current["status"] == "submission_unknown"
    assert current["submission_started"] is True
    assert current["request"] == original["request"]
    assert current["job_id"] is None
    assert weft_calls(calls) == ["run -C", "job lookup", "job fence"]

    if lookup == "absent":
        # A point-in-time absence cannot rule out acceptance of an in-flight request.
        (tmp_path / "lookup.json").write_text(
            json.dumps(
                {
                    "api_version": "weft.job.lookup.v1",
                    "idempotency_key": "slow",
                    "found": True,
                    "receipt": receipt("slow", "deduplicated"),
                }
            )
        )
        monkeypatch.setattr(tasks, "ensure_worker", REAL_ENSURE_WORKER)
        with pytest.raises(SystemExit) as recovered:
            cli.main(["task", "resume", "slow"])
        assert recovered.value.code == 0
        recovered_record = read_record("slow")
        assert (recovered_record["status"], recovered_record["job_id"]) == ("queued", "wj77")
        assert recovered_record["request"] == original["request"]
        assert weft_calls(calls) == ["run -C", "job lookup", "job fence", "job lookup"]


@pytest.mark.parametrize(
    ("status", "has_custody_marker"),
    [("submitting", True), ("submission_unknown", True), ("submitting", False)],
)
def test_cancel_cannot_discard_an_ambiguous_submission(
    tmp_path, monkeypatch, capsys, status, has_custody_marker
):
    from agent_offload import cli

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["unresolved"], lookup="found")
    with pytest.raises(TaskError):
        submit_slow(tmp_path)
    record = read_record("slow")
    assert record["submission_started"] is True
    record["status"] = status
    if not has_custody_marker:
        record.pop("submission_started")
    with locked("slow") as directory:
        tasks.save(directory / "task.json", record)
    with pytest.raises(SystemExit) as outcome:
        cli.main(["task", "cancel", "slow"])
    assert outcome.value.code == 1
    assert read_record("slow") == record
    assert weft_calls(calls) == ["run -C", "job fence"]


@pytest.mark.parametrize("custody", ["attempted", "unmarked"])
def test_fence_final_no_job_closes_attempt_without_erasing_custody(tmp_path, monkeypatch, custody):
    from agent_offload import cli

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["unresolved"], lookup="fail")
    with pytest.raises(TaskError):
        submit_slow(tmp_path)
    record = read_record("slow")
    assert record["submission_started"] is True
    if custody == "unmarked":
        # A lost marker is ambiguous; closure must not manufacture False custody.
        record.pop("submission_started")
        with locked("slow") as directory:
            tasks.save(directory / "task.json", record)
    (tmp_path / "fence.json").write_text(fence_answer(True))

    with pytest.raises(SystemExit) as result:
        cli.main(["task", "cancel", "slow"])
    assert result.value.code == 0

    cancelled = read_record("slow")
    assert cancelled["status"] == "cancelled"
    assert cancelled["processed_at"]
    assert cancelled["request"] == record["request"]
    assert cancelled["request_sha256"] == record["request_sha256"]
    payload = tmp_path / "state" / "tasks" / "slow" / "request.json"
    assert digest(payload.read_bytes()) == cancelled["request_sha256"]
    if custody == "unmarked":
        assert "submission_started" not in cancelled
    else:
        assert cancelled["submission_started"] is True
    assert weft_calls(calls) == ["run -C", "job fence"]

    # Repeated cancellation is idempotent and never contacts Weft again.
    with pytest.raises(SystemExit) as repeated:
        cli.main(["task", "cancel", "slow"])
    assert repeated.value.code == 0
    assert read_record("slow") == cancelled
    assert weft_calls(calls) == ["run -C", "job fence"]

    # The identity stays closed: resume cannot queue another Weft job.
    with pytest.raises(SystemExit) as resurrected:
        cli.main(["task", "resume", "slow"])
    assert resurrected.value.code == 1
    assert read_record("slow") == cancelled
    assert weft_calls(calls) == ["run -C", "job fence"]


@pytest.mark.parametrize(
    "answer",
    [
        "not JSON",
        '{"api_version": "wrong", "idempotency_key": "slow", "fenced": true}',
        fence_answer(True, key="other"),
        fence_answer(1),
        fence_answer(False),
        fence_answer(True, receipt=None),
        fence_answer(True, receipt=receipt("slow", "queued")),
        fence_answer(False, receipt={"api_version": "weft.run.receipt.v1"}),
        fence_answer(False, receipt=receipt("other", "queued")),
        fence_answer(False, receipt=receipt("slow", "not_accepted")),
    ],
    ids=[
        "malformed",
        "wrong-version",
        "other-key",
        "nonboolean-fenced",
        "receipt-without-job",
        "null-receipt-contradiction",
        "receipt-contradiction",
        "invalid-receipt",
        "mismatched-receipt",
        "unaccepted-receipt",
    ],
)
def test_cancel_keeps_custody_when_the_fence_answer_is_not_final(tmp_path, monkeypatch, answer):
    from agent_offload import cli

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["unresolved"], lookup="fail")
    with pytest.raises(TaskError):
        submit_slow(tmp_path)
    retained = read_record("slow")
    (tmp_path / "fence.json").write_text(answer)

    with pytest.raises(SystemExit) as outcome:
        cli.main(["task", "cancel", "slow"])
    assert outcome.value.code == 1

    # The whole retained record, request bytes included, is untouched.
    assert read_record("slow") == retained
    assert weft_calls(calls) == ["run -C", "job fence"]


@pytest.mark.parametrize("fence", ["unavailable", "hang"])
def test_cancel_keeps_custody_when_weft_fails_or_outlives_the_fence_budget(
    tmp_path, monkeypatch, capsys, fence
):
    from agent_offload import cli

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(tasks, "FENCE_TIMEOUT", 1)
    calls = fake_weft_submission(
        tmp_path, monkeypatch, runs=["unresolved"], lookup="fail", fence=fence
    )
    with pytest.raises(TaskError):
        submit_slow(tmp_path)
    retained = read_record("slow")

    with pytest.raises(SystemExit) as outcome:
        cli.main(["task", "cancel", "slow"])
    assert outcome.value.code == 1

    # The failure stays visible and names both ways forward.
    diagnostic = capsys.readouterr().err
    assert "agent-offload task cancel slow" in diagnostic
    assert "agent-offload task resume slow" in diagnostic
    assert read_record("slow") == retained
    assert weft_calls(calls) == ["run -C", "job fence"]


def test_fence_addresses_the_current_attempt_key_in_the_project_cwd(tmp_path, monkeypatch):
    from agent_offload import cli

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["unresolved"], lookup="fail")
    with pytest.raises(TaskError):
        submit_slow(tmp_path)
    # Rotate the attempt the way a resubmission would; the fence must address
    # that attempt's key, not the bare identity.
    record = read_record("slow")
    record["attempt"] = 2
    with locked("slow") as directory:
        tasks.save(directory / "task.json", record)
    (tmp_path / "fence.json").write_text(fence_answer(True, key="slow-2"))

    with pytest.raises(SystemExit) as outcome:
        cli.main(["task", "cancel", "slow"])
    assert outcome.value.code == 0

    assert "job fence --idempotency-key slow-2 --json" in calls.read_text()
    assert weft_calls(calls) == ["run -C", "job fence"]
    project = Path(record["project"])
    assert Path((tmp_path / "fence-cwd").read_text().strip()).resolve() == project.resolve()
    assert read_record("slow")["status"] == "cancelled"


@pytest.mark.parametrize("stage", ["cancel", "ack"])
def test_a_fenced_receipt_stays_bound_when_cancellation_or_acknowledgment_fails(
    tmp_path, monkeypatch, stage
):
    from agent_offload import cli

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["unresolved"], lookup="fail")
    with pytest.raises(TaskError):
        submit_slow(tmp_path)
    original = read_record("slow")
    (tmp_path / "fence.json").write_text(fence_answer(False, receipt=receipt("slow", "queued")))
    (tmp_path / "job.json").write_text(json.dumps({"id": "wj77", "status": "queued"}))
    # The fake names both its refusal switch and its capture after the refused command.
    refused = "cancel" if stage == "cancel" else "mark"
    (tmp_path / f"refuse-{refused}").write_text("refuse\n")

    with pytest.raises(SystemExit) as outcome:
        cli.main(["task", "cancel", "slow"])
    assert outcome.value.code == 1

    # The recovered identity was already durable when the failing call ran.
    recovered = ("wj77", "studio", "pin")
    at_failure = json.loads((tmp_path / f"record-at-{refused}.json").read_text())
    assert (at_failure["job_id"], at_failure["host"], at_failure["source_pin"]) == recovered
    assert "processed_at" not in at_failure
    assert at_failure["request"] == original["request"]
    assert at_failure["request_sha256"] == original["request_sha256"]
    retained = read_record("slow")
    assert (retained["job_id"], retained["host"], retained["source_pin"]) == recovered
    assert "processed_at" not in retained
    if stage == "cancel":
        assert weft_calls(calls) == ["run -C", "job fence", "job inspect", "cancel wj77"]
    else:
        assert json.loads((tmp_path / "job.json").read_text())["status"] == "cancelled"
        assert weft_calls(calls) == [
            "run -C",
            "job fence",
            "job inspect",
            "cancel wj77",
            "job inspect",
            "job mark-processed",
        ]

    # Clearing the refusal retries only the owed disposition, without another fence.
    (tmp_path / "refuse-cancel").unlink(missing_ok=True)
    (tmp_path / "refuse-mark").unlink(missing_ok=True)
    with pytest.raises(SystemExit) as retried:
        cli.main(["task", "cancel", "slow"])
    assert retried.value.code == 0
    assert read_record("slow")["processed_at"]
    assert weft_calls(calls).count("job fence") == 1


@pytest.mark.parametrize("custody", ["attempted", "unmarked"])
@pytest.mark.parametrize("job_status", ["queued", "canceled"])
def test_cancel_binds_a_fenced_receipt_then_disposes_the_job(
    tmp_path, monkeypatch, capsys, custody, job_status
):
    from agent_offload import cli

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["unresolved"], lookup="fail")
    with pytest.raises(TaskError):
        submit_slow(tmp_path)
    record = read_record("slow")
    if custody == "unmarked":
        record.pop("submission_started")
        with locked("slow") as directory:
            tasks.save(directory / "task.json", record)
    (tmp_path / "fence.json").write_text(fence_answer(False, receipt=receipt("slow", "queued")))
    (tmp_path / "job.json").write_text(json.dumps({"id": "wj77", "status": job_status}))

    with pytest.raises(SystemExit) as outcome:
        cli.main(["task", "cancel", "slow"])
    assert outcome.value.code == 0

    row = json.loads(capsys.readouterr().out)
    assert (row["status"], row["job_id"]) == (
        "cancelled" if job_status == "queued" else "canceled",
        "wj77",
    )
    bound = read_record("slow")
    assert (bound["job_id"], bound["host"], bound["source_pin"]) == ("wj77", "studio", "pin")
    assert bound["status"] == "queued"
    assert bound["processed_at"]
    assert bound["request"] == record["request"]
    assert bound["request_sha256"] == record["request_sha256"]
    if custody == "unmarked":
        assert "submission_started" not in bound
    else:
        assert bound["submission_started"] is True
    # Read-only `job inspect` calls are incidental; only the side effects are pinned.
    side_effects = ("job fence", "cancel wj77", "job mark-processed")
    steps = weft_calls(calls)
    assert steps[:2] == ["run -C", "job fence"]
    disposed = [step for step in steps if step in side_effects]
    if job_status == "queued":
        # A live job is fenced once, then cancelled before its terminal acknowledgment.
        assert disposed == ["job fence", "cancel wj77", "job mark-processed"]
    else:
        # A job Weft already ended is acknowledged without another cancellation.
        assert disposed == ["job fence", "job mark-processed"]

    # The disposition is owed once: a repeat neither re-fences, cancels, nor re-acknowledges.
    with pytest.raises(SystemExit) as repeated:
        cli.main(["task", "cancel", "slow"])
    assert repeated.value.code == 0
    assert read_record("slow") == bound
    assert [step for step in weft_calls(calls) if step in side_effects] == disposed


# ---------------------------------------------------------------------------
# Submission-stage and custody diagnostics (consumer-visible)
# ---------------------------------------------------------------------------


def minimal_record(identity: str) -> dict:
    """A record shaped like one an older release wrote: no new fields."""
    return {
        "schema_version": "agent-offload.task/v1",
        "id": identity,
        "project": "/project",
        "host": "studio",
        "title": identity,
        "created_at": 1.0,
        "status": "submitting",
        "submission_started": False,
        "job_id": None,
    }


def status_row(identity: str, capsys) -> dict:
    from agent_offload import cli

    with pytest.raises(SystemExit) as shown:
        cli.main(["task", "status", identity])
    assert shown.value.code == 0
    return json.loads(capsys.readouterr().out)


def test_the_durable_stage_is_visible_while_provisioning_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    fake_weft_submission(tmp_path, monkeypatch, runs=["queued"], lookup="fail")
    observed = {}

    def slow_provision(record):
        # Whatever a crash here left on disk is what consumers must see.
        retained = read_record(record["id"])
        observed["record"] = retained
        observed["view"] = tasks.summary(retained)["submission"]
        record["bin_dir"], record["build_id"] = PINNED_BIN, PINNED_BUILD

    monkeypatch.setattr(tasks, "ensure_worker", slow_provision)
    submit_slow(tmp_path)

    assert observed["record"]["provisioning"] == "in_progress"
    view = observed["view"]
    assert (view["stage"], view["provisioning"]) == ("provisioning_in_progress", "in_progress")
    assert view["submission_started"] is False
    assert view["job_id"] is None

    view = tasks.summary(read_record("slow"))["submission"]
    assert (view["stage"], view["provisioning"]) == ("accepted", "ready")
    assert (view["job_id"], view["build_id"]) == ("wj77", PINNED_BUILD)
    assert view["submission_started"] is True


def test_a_provisioning_failure_keeps_the_identity_and_resume_retries_it(
    tmp_path, monkeypatch, capsys
):
    from agent_offload import cli

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["queued"], lookup="fail")

    def unreachable(record):
        raise TaskError("ssh: host unreachable")

    monkeypatch.setattr(tasks, "ensure_worker", unreachable)
    with pytest.raises(TaskError) as failed:
        submit_slow(tmp_path)

    message = str(failed.value)
    assert "task slow" in message and "ssh: host unreachable" in message
    assert "agent-offload task resume slow" in message
    assert not calls.exists()

    row = status_row("slow", capsys)
    view = row["submission"]
    assert (view["stage"], view["provisioning"]) == ("provisioning_failed", "failed")
    assert view["provisioning_error"] == "ssh: host unreachable"
    assert view["job_id"] is None and view["submission_started"] is False

    def pinned(record):
        record["bin_dir"], record["build_id"] = PINNED_BIN, PINNED_BUILD

    monkeypatch.setattr(tasks, "ensure_worker", pinned)
    with pytest.raises(SystemExit) as resumed:
        cli.main(["task", "resume", "slow"])
    assert resumed.value.code == 0
    row = json.loads(capsys.readouterr().out)
    assert (row["status"], row["job_id"]) == ("queued", "wj77")
    view = row["submission"]
    assert (view["stage"], view["provisioning"]) == ("accepted", "ready")
    assert "provisioning_error" not in view


def test_a_pin_the_install_cannot_satisfy_refuses_without_resume_guidance(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["queued"], lookup="fail")

    def unavailable(record):
        raise worker_build.PinnedBuildUnavailable(
            "the installed agent-offload changed since this task was recorded; "
            "submit a new task for the same brief"
        )

    monkeypatch.setattr(tasks, "ensure_worker", unavailable)
    with pytest.raises(TaskError) as failed:
        submit_slow(tmp_path)

    message = str(failed.value)
    assert "task slow" in message
    assert "submit a new task for the same brief" in message
    # Resuming cannot rebuild bytes this install no longer has: no such advice.
    assert "resume with:" not in message
    assert not calls.exists()

    view = tasks.summary(read_record("slow"))["submission"]
    assert (view["stage"], view["provisioning"]) == ("provisioning_failed", "failed")
    assert "changed since this task was recorded" in view["provisioning_error"]


def test_an_admission_refusal_leaves_the_worker_ready_stage_with_retained_error(
    tmp_path, monkeypatch, capsys
):
    from agent_offload import cli
    from agent_offload.readiness import require_writer

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(tasks, "require_writer", require_writer)
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["queued"], lookup="absent")
    observe, _ = fake_writer_readiness(tmp_path, monkeypatch)
    observe(credential_available=False, detail="No execution-eligible OMP credentials")

    with pytest.raises(TaskError):
        submit_slow(tmp_path)

    assert not calls.exists()
    row = status_row("slow", capsys)
    view = row["submission"]
    assert (view["stage"], view["provisioning"]) == ("worker_ready", "ready")
    assert view["job_id"] is None and view["submission_started"] is False
    assert "No execution-eligible OMP credentials" in row["error"]

    observe()
    with pytest.raises(SystemExit) as resumed:
        cli.main(["task", "resume", "slow"])
    assert resumed.value.code == 0
    row = json.loads(capsys.readouterr().out)
    assert (row["status"], row["job_id"]) == ("queued", "wj77")
    assert row["submission"]["stage"] == "accepted"
    assert row["error"] is None


def test_an_ambiguous_send_reads_possibly_accepted_until_resolved(tmp_path, monkeypatch, capsys):
    from agent_offload import cli

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    fake_weft_submission(tmp_path, monkeypatch, runs=["unresolved"], lookup="fail")
    with pytest.raises(TaskError):
        submit_slow(tmp_path)

    view = status_row("slow", capsys)["submission"]
    assert view["stage"] == "submission_possibly_accepted"
    assert view["submission_started"] is True
    assert view["job_id"] is None  # a null identity is never proof of refusal

    # Weft's final no-job fence closes the attempt, and the closure says why.
    (tmp_path / "fence.json").write_text(fence_answer(True))
    with pytest.raises(SystemExit) as closed:
        cli.main(["task", "cancel", "slow"])
    assert closed.value.code == 0
    view = json.loads(capsys.readouterr().out)["submission"]
    assert view["stage"] == "cancelled_no_job"
    assert view["cancelled_because"] == "weft_reported_no_job"
    assert read_record("slow")["cancelled_without_job"] == "weft_reported_no_job"


def test_receipt_recovery_turns_a_possible_acceptance_into_an_accepted_identity(
    tmp_path, monkeypatch, capsys
):
    from agent_offload import cli

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["unresolved"], lookup="found")
    with pytest.raises(TaskError):
        submit_slow(tmp_path)
    assert tasks.summary(read_record("slow"))["submission"]["stage"] == (
        "submission_possibly_accepted"
    )

    # Reconciliation precedes provisioning, so the accepted job binds even
    # though a real provisioning call would fail under this restore.
    monkeypatch.setattr(tasks, "ensure_worker", REAL_ENSURE_WORKER)
    with pytest.raises(SystemExit) as recovered:
        cli.main(["task", "resume", "slow"])
    assert recovered.value.code == 0
    row = json.loads(capsys.readouterr().out)
    assert (row["status"], row["job_id"]) == ("queued", "wj77")
    assert row["submission"]["stage"] == "accepted"
    assert weft_calls(calls) == ["run -C", "job lookup"]


def test_a_never_submitted_task_cancels_with_a_proven_never_submitted_stage(
    tmp_path, monkeypatch, capsys
):
    from agent_offload.readiness import require_writer

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(tasks, "require_writer", require_writer)
    calls = fake_weft_submission(tmp_path, monkeypatch, runs=["queued"], lookup="absent")
    observe, _ = fake_writer_readiness(tmp_path, monkeypatch)
    observe(model_available=False, detail="Exact model absent")
    with pytest.raises(TaskError):
        submit_slow(tmp_path)

    from agent_offload import cli

    with pytest.raises(SystemExit) as closed:
        cli.main(["task", "cancel", "slow"])
    assert closed.value.code == 0

    row = json.loads(capsys.readouterr().out)
    view = row["submission"]
    assert view["stage"] == "cancelled_no_job"
    assert view["cancelled_because"] == "never_submitted"
    assert view["job_id"] is None and view["submission_started"] is False
    assert read_record("slow")["cancelled_without_job"] == "never_submitted"
    assert not calls.exists()
    # The identity stays closed; resume cannot resurrect it.
    with pytest.raises(SystemExit) as resurrected:
        cli.main(["task", "resume", "slow"])
    assert resurrected.value.code == 1
    assert read_record("slow")["status"] == "cancelled"


def test_legacy_records_report_unknown_instead_of_invented_stages(tmp_path, monkeypatch, capsys):
    from agent_offload import cli

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    with locked("old-unprovisioned") as directory:
        tasks.save(directory / "task.json", minimal_record("old-unprovisioned"))
    with locked("old-pinned") as directory:
        record = minimal_record("old-pinned")
        record.update(bin_dir=PINNED_BIN, build_id=PINNED_BUILD)
        tasks.save(directory / "task.json", record)
    with locked("old-cancelled") as directory:
        # A jobless closure whose proof the record does not carry.
        record = minimal_record("old-cancelled")
        record.update(status="cancelled")
        record.pop("submission_started")
        tasks.save(directory / "task.json", record)

    unprovisioned = status_row("old-unprovisioned", capsys)["submission"]
    # Nothing proves where this submission stopped, so nothing claims to.
    assert unprovisioned["stage"] == "unknown"
    assert unprovisioned["provisioning"] == "unknown"
    assert unprovisioned["submission_started"] is False

    pinned = status_row("old-pinned", capsys)["submission"]
    # A retained build identity proves provisioning without the field.
    assert (pinned["stage"], pinned["provisioning"]) == ("worker_ready", "ready")
    assert pinned["build_id"] == PINNED_BUILD

    cancelled = status_row("old-cancelled", capsys)["submission"]
    assert cancelled["stage"] == "unknown"
    assert "cancelled_because" not in cancelled

    with pytest.raises(SystemExit) as listed:
        cli.main(["task", "list"])
    assert listed.value.code == 0
    rows = {row["id"]: row for row in json.loads(capsys.readouterr().out)["tasks"]}
    assert rows["old-pinned"]["submission"]["stage"] == "worker_ready"
    assert rows["old-unprovisioned"]["submission"]["stage"] == "unknown"
