from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_offload.task_protocol import (
    TaskError,
    encode,
    manifest,
    render_diff,
    safe_path,
    validate_changes,
)
from agent_offload.tasks import apply_changes, locked, read_record, submit


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


def test_submission_timeout_retains_identity_and_request(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.py").write_text("hello")
    # External executables are simulated; the task's submission and persistence
    # code run unchanged, including the failure path after payload publication.
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, content in {
        "rg": "#!/bin/sh\nprintf 'hello.py\\n'\n",
        "weft": "#!/bin/sh\necho 'transport lost after acceptance' >&2\nexit 1\n",
    }.items():
        path = tools / name
        path.write_text(content)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
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
    if refused:
        with pytest.raises(TaskError, match="validation modified proposed source"):
            task_worker.execute(request, tmp_path)
    else:
        result = task_worker.execute(request, tmp_path)
        assert result["status"] == "completed"
        assert result["changes"] == changes
