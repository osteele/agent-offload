from __future__ import annotations

import json
import shlex
from itertools import pairwise
from pathlib import Path

import pytest

from agent_offload import tasks
from agent_offload.task_protocol import (
    TaskError,
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
    request = {"baseline": manifest(root, ["hello.py"])}
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


def fake_weft_submission(tmp_path, monkeypatch, *, runs: list[str], lookup: str) -> Path:
    """Fake weft whose successive `weft run` calls follow `runs`, logging every call.

    Each run is "hang" (outlive the submit budget) or "queued" (print a receipt
    for a job left to the daemon, as --no-sync does). `lookup` is "found",
    "absent", "other-key" (an answer about a different key), or "fail" for
    `weft job lookup`.
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
    lookup_step = (
        "echo 'ledger unreadable' >&2; exit 1" if lookup == "fail" else f'/bin/cat "{answer}"'
    )
    run_steps = "".join(
        f"  {index}) {'exec /bin/sleep 5' if step == 'hang' else f'/bin/cat {queued}'} ;;\n"
        for index, step in enumerate(runs, start=1)
    )
    fake_tools(
        tmp_path,
        monkeypatch,
        files=["hello.py"],
        excludes=[".git"],
        run=(
            f'echo "$*" >> "{calls}"\n'
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


def test_submit_refuses_a_stale_bundled_agent_execution(tmp_path, monkeypatch):
    project = bare_project(tmp_path, monkeypatch)
    fake_tools(tmp_path, monkeypatch, files=["hello.py"], excludes=[], run="exit 1")
    identity_stub(tmp_path, "0" * 64)
    with pytest.raises(TaskError, match="bundled into this agent-offload") as refusal:
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity="stale",
        )
    assert "uv tool install" in str(refusal.value)
    assert not (tmp_path / "state" / "tasks" / "stale").exists()


def test_submit_accepts_an_installed_agent_execution_matching_the_bundle(tmp_path, monkeypatch):
    from agent_execution.identity import source_sha256

    project = bare_project(tmp_path, monkeypatch)
    fake_tools(tmp_path, monkeypatch, files=["hello.py"], excludes=[], run="exit 1")
    identity_stub(tmp_path, source_sha256())
    # The weft stub refuses every run, so reaching it proves the probe passed.
    with pytest.raises(TaskError, match="resume this identity"):
        submit(
            project,
            "Fix hello",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity="matching",
        )


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
    assert "agent-host-sync workspace run --dependency" in str(refusal.value)
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
