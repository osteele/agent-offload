from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

from agent_offload import worker
from agent_offload.protocol import WORKER_PROTOCOL_VERSION, worker_identity


def completed_harness(task_id: str, prompt_sha256: str, report: str) -> dict[str, object]:
    return {
        "schema_version": "agent-review.worker-result/v2",
        "worker_protocol_version": 6,
        "status": "completed",
        "model_call_id": task_id,
        "provider": "codex",
        "prompt_sha256": prompt_sha256,
        "session_id": "thread-123",
        "worker_identity": {
            "schema_version": "agent-review.worker-identity/v1",
            "protocol_version": 6,
            "source_sha256": "a" * 64,
        },
        "harness": {
            "exit_status": 0,
            "stderr": "",
            "stdout": "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": report},
                        }
                    ),
                ]
            ),
        },
        "failure": "",
    }


def test_extract_codex_report_uses_last_agent_message() -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "draft"}}
            ),
            "not json",
            json.dumps(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}}
            ),
        ]
    )
    assert worker.extract_codex_report(stdout) == "final"


def test_remote_nested_command_hides_model_from_weft_scanner(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        worker.shutil,
        "which",
        lambda name: "/tools/agent-review-worker" if name == "agent-review-worker" else None,
    )
    monkeypatch.setattr(worker, "resolve_codex", lambda: "/tools/codex")
    command = worker.nested_command(
        task_id="task-1",
        selector="gpt-5.5",
        effort="medium",
        prompt_payload="agent-brief",
        prompt_sha256="a" * 64,
        harness_result_path=tmp_path / "harness.json",
        harness_timeout=10,
        ctx_timeout=5,
    )
    assert "--model" not in command
    assert command[0] == "/tools/agent-review-worker"
    separator = command.index("--")
    assert command[separator + 1] == "/tools/codex"
    assert command[command.index("--harness-model") + 1] == "gpt-5.5"
    assert shlex.join(command).endswith("-")


def test_execute_writes_validated_report_and_full_harness(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    prompt = "Inspect this project and report one risk.\n"
    (payload_dir / "agent-brief").write_text(prompt)
    monkeypatch.setenv("WEFT_PAYLOAD_DIR", str(payload_dir))
    monkeypatch.setattr(
        worker.shutil,
        "which",
        lambda name: "/tools/agent-review-worker" if name == "agent-review-worker" else None,
    )
    monkeypatch.setattr(worker, "resolve_codex", lambda: "/tools/codex")
    prompt_sha256 = worker.sha256_text(prompt)

    def fake_runner(command, **_kwargs):
        evidence_path = Path(command[command.index("--evidence-out") + 1])
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(completed_harness("task-1", prompt_sha256, "Risk: stale schema."))
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    identity = worker_identity()
    result, result_path, report_path = worker.execute(
        task_id="task-1",
        provider="codex",
        mode="report",
        selector="gpt-5.5",
        effort="medium",
        prompt_payload="agent-brief",
        prompt_sha256=prompt_sha256,
        expect_protocol=WORKER_PROTOCOL_VERSION,
        expect_source_sha256=str(identity["source_sha256"]),
        harness_timeout=10,
        ctx_timeout=5,
        runner=fake_runner,
    )

    assert result["status"] == "completed"
    assert result["harness_session_id"] == "thread-123"
    assert result["harness_result"]["harness"]["stdout"]
    assert report_path.read_text() == "Risk: stale schema.\n"
    assert json.loads(result_path.read_text())["report"] == "Risk: stale schema."


def test_execute_rejects_a_report_after_source_mutation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source.txt").write_text("before\n")
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    prompt = "Inspect source.txt.\n"
    (payload_dir / "agent-brief").write_text(prompt)
    monkeypatch.setenv("WEFT_PAYLOAD_DIR", str(payload_dir))
    monkeypatch.setattr(
        worker.shutil,
        "which",
        lambda name: "/tools/agent-review-worker" if name == "agent-review-worker" else None,
    )
    monkeypatch.setattr(worker, "resolve_codex", lambda: "/tools/codex")
    prompt_sha256 = worker.sha256_text(prompt)

    def fake_runner(command, **_kwargs):
        (tmp_path / "source.txt").write_text("after\n")
        evidence_path = Path(command[command.index("--evidence-out") + 1])
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(completed_harness("task-3", prompt_sha256, "The file changed."))
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result, _result_path, report_path = worker.execute(
        task_id="task-3",
        provider="codex",
        mode="report",
        selector="gpt-5.5",
        effort="medium",
        prompt_payload="agent-brief",
        prompt_sha256=prompt_sha256,
        expect_protocol=WORKER_PROTOCOL_VERSION,
        expect_source_sha256=str(worker_identity()["source_sha256"]),
        harness_timeout=10,
        ctx_timeout=5,
        runner=fake_runner,
    )
    assert result["status"] == "evidence_failed"
    assert result["report"] is None
    assert result["source_sha256_before"] != result["source_sha256_after"]
    assert "modified the disposable source snapshot" in report_path.read_text()


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
