from __future__ import annotations

from pathlib import Path

from agent_offload import worker
from agent_offload.protocol import (
    NATIVE_DISPATCH_RETIRED,
    WORKER_PROTOCOL_VERSION,
    worker_identity,
)


def test_execute_refuses_retired_native_codex_before_launch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    launched = False

    def fake_runner(*_args, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("must not launch")

    result, result_path, report_path = worker.execute(
        task_id="task-retired",
        provider="codex",
        mode="report",
        selector="gpt-5.5",
        effort="medium",
        prompt_payload="agent-brief",
        prompt_sha256="a" * 64,
        expect_protocol=WORKER_PROTOCOL_VERSION,
        expect_source_sha256=str(worker_identity()["source_sha256"]),
        harness_timeout=10,
        ctx_timeout=5,
        runner=fake_runner,
    )

    assert not launched
    assert result["status"] == "preflight_failed"
    assert result["failure"] == NATIVE_DISPATCH_RETIRED
    assert result_path.is_file()
    assert NATIVE_DISPATCH_RETIRED in report_path.read_text()


def test_execute_refuses_source_drift_before_launch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    launched = False

    def fake_runner(*_args, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("must not launch")

    result, result_path, report_path = worker.execute(
        task_id="task-2",
        provider="codex",
        mode="report",
        selector="gpt-5.5",
        effort="medium",
        prompt_payload="agent-brief",
        prompt_sha256="b" * 64,
        expect_protocol=WORKER_PROTOCOL_VERSION,
        expect_source_sha256="0" * 64,
        harness_timeout=10,
        ctx_timeout=5,
        runner=fake_runner,
    )

    assert not launched
    assert result["status"] == "preflight_failed"
    assert "source mismatch" in result["failure"]
    assert result_path.is_file()
    assert report_path.read_text().startswith("# Agent offload failed\n\nworker source mismatch:")


def test_source_snapshot_ignores_worker_evidence_but_detects_source_edits(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("original\n")
    before = worker.source_tree_sha256(tmp_path)

    evidence = tmp_path / ".agent-execution" / "results" / "task-1.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"status":"completed"}\n')
    assert worker.source_tree_sha256(tmp_path) == before

    source.write_text("modified\n")
    assert worker.source_tree_sha256(tmp_path) != before
