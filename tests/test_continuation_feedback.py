"""Regressions for continuation selection, custody, and immutable provenance."""

import argparse
import json

from agent_execution.omp_execution import OMP_WRITER_SELECTORS
from test_task_continuation import (
    capture_project,
    continue_args,
)
from test_task_continuation import (
    task_factory as parent_task_factory,
)

task_factory = parent_task_factory

from agent_offload import tasks
from agent_offload.task_protocol import canonical_digest


def alternate_selector():
    return min(OMP_WRITER_SELECTORS - {tasks.DEFAULT_SELECTOR})


def test_retrieved_artifact_digest_includes_artifact_review_not_local_annotations(
    task_factory, tmp_path
):
    record, result = task_factory("artifact-review")
    result["review"] = {"artifact-owned": "must remain in provenance"}
    replies = json.loads((tmp_path / "replies.json").read_text())
    replies[record["job_id"]]["result"] = result
    (tmp_path / "replies.json").write_text(json.dumps(replies))
    first = tasks.retrieve(record, include_diff=False, include_artifact_digest=True)
    assert first["review"] != result["review"]
    assert first["_artifact_sha256"] == canonical_digest(result)
    second = tasks.retrieve(record, include_diff=True, include_artifact_digest=True)
    assert second["_artifact_sha256"] == first["_artifact_sha256"]
    assert second["review"] == first["review"]
    assert first["diff"] == ""


def test_selected_writer_is_retained_and_invalid_selection_creates_no_child(
    task_factory, tmp_path, monkeypatch, capsys
):
    capture_project(tmp_path, monkeypatch)
    parent, _ = task_factory("selector-parent", "validation_failed")
    target = alternate_selector()
    # Keep the remote setup side effects deterministic while preserving the
    # real durable dispatch/send path.
    monkeypatch.setattr(
        tasks, "ensure_worker", lambda record: record.update(bin_dir="/worker", build_id="build")
    )
    monkeypatch.setattr(tasks, "require_writer", lambda record: None)
    assert (
        tasks.handler(
            argparse.Namespace(
                **{**vars(continue_args(parent["id"], id="selected-child")), "selector": target}
            )
        )
        == 0
    )
    capsys.readouterr()
    assert tasks.read_record("selected-child")["request"]["selector"] == target
    before_runs = len(__import__("test_task_continuation").weft_runs(tmp_path))
    assert (
        tasks.handler(
            argparse.Namespace(
                **{
                    **vars(continue_args(parent["id"], id="invalid-child")),
                    "selector": "not/admitted",
                }
            )
        )
        == 1
    )
    assert "not admitted" in capsys.readouterr().err
    assert not (tasks.state_root() / "invalid-child").exists()
    assert len(__import__("test_task_continuation").weft_runs(tmp_path)) == before_runs


def test_default_writer_is_inherited(task_factory, tmp_path, monkeypatch, capsys):
    capture_project(tmp_path, monkeypatch)
    parent, _ = task_factory("default-selector-parent", "validation_failed")
    monkeypatch.setattr(
        tasks, "ensure_worker", lambda record: record.update(bin_dir="/worker", build_id="build")
    )
    monkeypatch.setattr(tasks, "require_writer", lambda record: None)
    assert tasks.handler(continue_args(parent["id"], id="default-selector-child")) == 0
    capsys.readouterr()
    assert (
        tasks.read_record("default-selector-child")["request"]["selector"]
        == parent["request"]["selector"]
    )


def test_unavailable_selected_writer_retains_child_then_same_id_resume_recovers(
    task_factory, tmp_path, monkeypatch, capsys
):
    capture_project(tmp_path, monkeypatch)
    parent, _ = task_factory("admission-parent", "validation_failed")
    monkeypatch.setattr(
        tasks, "ensure_worker", lambda record: record.update(bin_dir="/worker", build_id="build")
    )
    monkeypatch.setattr(
        tasks,
        "require_writer",
        lambda record: (_ for _ in ()).throw(tasks.TaskError("writer unavailable")),
    )
    target = alternate_selector()
    assert (
        tasks.handler(
            argparse.Namespace(
                **{**vars(continue_args(parent["id"], id="admission-child")), "selector": target}
            )
        )
        == 1
    )
    assert "resume with" in capsys.readouterr().err
    child = tasks.read_record("admission-child")
    assert child["request"]["selector"] == target
    assert child["job_id"] is None
    assert __import__("test_task_continuation").weft_runs(tmp_path) == []
    monkeypatch.setattr(tasks, "require_writer", lambda record: None)
    assert tasks.handler(argparse.Namespace(action="resume", id="admission-child")) == 0
    capsys.readouterr()
    recovered = tasks.read_record("admission-child")
    assert recovered["request"]["selector"] == target
    assert recovered["job_id"]
    assert [
        call[call.index("--idempotency-key") + 1]
        for call in __import__("test_task_continuation").weft_runs(tmp_path)
    ] == ["admission-child"]


def test_retired_parent_writer_can_be_overridden(task_factory, tmp_path, monkeypatch, capsys):
    capture_project(tmp_path, monkeypatch)
    parent, _ = task_factory("retired-parent", "validation_failed")
    target = alternate_selector()
    monkeypatch.setattr(tasks, "OMP_WRITER_SELECTORS", frozenset({target}))
    monkeypatch.setattr(
        tasks, "ensure_worker", lambda record: record.update(bin_dir="/worker", build_id="build")
    )
    monkeypatch.setattr(tasks, "require_writer", lambda record: None)
    assert (
        tasks.handler(
            argparse.Namespace(
                **{**vars(continue_args(parent["id"], id="retired-child")), "selector": target}
            )
        )
        == 0
    )
    capsys.readouterr()
    assert tasks.read_record("retired-child")["request"]["selector"] == target
