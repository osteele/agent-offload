from __future__ import annotations

import json
import shlex
from types import SimpleNamespace

import pytest
from agent_execution.worker import installed_worker_identity

from agent_offload import cli
from agent_offload.protocol import RESULT_SCHEMA, sha256_text, worker_identity


def completed_result(task_id: str, report: str) -> dict[str, object]:
    prompt_sha256 = "a" * 64
    execution_identity = installed_worker_identity()
    return {
        "schema_version": RESULT_SCHEMA,
        "task_id": task_id,
        "provider": "codex",
        "mode": "report",
        "selector": "gpt-5.5",
        "effort": "medium",
        "status": "completed",
        "prompt_sha256": prompt_sha256,
        "worker_identity": worker_identity(),
        "harness_result": {
            "schema_version": "agent-execution.worker-result/v1",
            "model_call_id": task_id,
            "provider": "codex",
            "status": "completed",
            "worker_version": execution_identity.package_version,
            "worker_protocol_version": execution_identity.protocol_version,
            "worker_source_sha256": execution_identity.source_sha256,
            "prompt_sha256": prompt_sha256,
            "ctx_version": "ctx 1.0.0",
            "worker_cwd": "/project",
            "started_at": 1.0,
            "completed_at": 2.0,
            "model_call_started": True,
            "session_id": "thread-1",
            "harness": {"exit_status": 0, "stdout": "{}", "stderr": ""},
            "evidence": {
                "import_receipt": {"schema_version": 2},
                "transcript": {"schema_version": 1},
            },
            "failure": "",
            "worker_identity": execution_identity.to_dict(),
        },
        "harness_session_id": "thread-1",
        "report": report,
        "report_sha256": sha256_text(report),
        "source_sha256_before": "c" * 64,
        "source_sha256_after": "c" * 64,
        "report_path": f"outputs/agent-offload-report-{task_id}.md",
        "failure": "",
        "started_at": 1.0,
        "completed_at": 2.0,
        "worker_cwd": "/project",
    }


def test_receipt_rejects_wrong_idempotency_key() -> None:
    payload = json.dumps(
        {
            "api_version": "weft.run.receipt.v1",
            "job_id": "wj1",
            "placement_decision": "accepted_immediately",
            "selected_host": "studio",
            "source_pin": "sha256:abc",
            "accepted_immediately": True,
            "deduplicated": False,
            "idempotency_key": "different",
        }
    )
    with pytest.raises(ValueError, match="idempotency"):
        cli.WeftReceipt.parse(payload, idempotency_key="task-1")


def test_remote_command_uses_report_only_worker_protocol() -> None:
    command = shlex.split(
        cli.remote_command(
            task_id="task-1",
            selector="gpt-5.5",
            effort="high",
            prompt_sha256="a" * 64,
            source_sha256="b" * 64,
            harness_timeout=10,
            ctx_timeout=5,
        )
    )
    assert command[:3] == ["python3", "src/agent_offload/weft_entry.py", "execute"]
    assert command[command.index("--mode") + 1] == "report"
    assert command[command.index("--harness-selector") + 1] == "gpt-5.5"
    assert "--model" not in command


def test_doctor_requires_declared_tool_and_observed_authentication(monkeypatch) -> None:
    responses = iter(
        [
            {
                "kind": "host_list",
                "version": 1,
                "hosts": [
                    {
                        "name": "studio",
                        "capabilities": [
                            "agent:codex",
                            "tool:agent-offload",
                            "tool:agent-execution",
                        ],
                    }
                ],
            },
            {
                "observations": [
                    {
                        "label": "agent:codex",
                        "state": "locally_authenticated",
                        "detail": "Codex login exists",
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(cli, "command_json", lambda *_args, **_kwargs: next(responses))
    result = cli.doctor(host="studio", provider="codex")
    assert result["available"] is True
    assert [check["name"] for check in result["checks"]] == ["inventory", "authentication"]


def test_doctor_refuses_declared_but_unauthenticated_provider(monkeypatch) -> None:
    responses = iter(
        [
            {
                "kind": "host_list",
                "version": 1,
                "hosts": [
                    {
                        "name": "studio",
                        "capabilities": [
                            "agent:codex",
                            "tool:agent-offload",
                            "tool:agent-execution",
                        ],
                    }
                ],
            },
            {
                "observations": [
                    {
                        "label": "agent:codex",
                        "state": "declared_but_not_observed",
                        "detail": "no authentication evidence",
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(cli, "command_json", lambda *_args, **_kwargs: next(responses))
    assert cli.doctor(host="studio", provider="codex")["available"] is False


def test_build_prompt_enforces_report_only_behavior() -> None:
    prompt = cli.build_prompt("Inspect the parser.")
    assert "Do not edit files" in prompt
    assert prompt.endswith("User brief:\nInspect the parser.\n")


def test_wait_uses_weft_first_class_wait_and_one_artifact_sync(monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run_process(arguments, **_kwargs):
        commands.append(list(arguments))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(cli, "run_process", fake_run_process)
    monkeypatch.setattr(
        cli,
        "inspect_job",
        lambda _job_id: {"status": "completed", "project": "agent-offload"},
    )
    monkeypatch.setattr(
        cli, "retrieve", lambda *_args, **_kwargs: {"result": {"status": "completed"}}
    )
    receipt = cli.WeftReceipt(
        job_id="wj1",
        placement_decision="accepted_immediately",
        selected_host="studio",
        source_pin="pin",
        accepted_immediately=True,
        deduplicated=False,
        idempotency_key="task-1",
    )
    cli.wait_for_result(
        receipt,
        task_id="task-1",
        prompt_sha256="a" * 64,
        source_sha256="b" * 64,
        timeout=30,
        mark_processed=True,
    )
    assert commands == [
        ["weft", "status", "wj1", "--wait", "--wait-timeout", "30s", "--json"],
        ["weft", "artifact", "sync", "wj1"],
    ]


def test_retrieve_validates_both_artifacts_before_marking_processed(monkeypatch) -> None:
    task_id = "task-1"
    result = completed_result(task_id, "Finding: schema drift.")
    inspection = {
        "id": "wj1",
        "status": "completed",
        "project": "agent-offload",
        "produces": [
            f"outputs/agent-offload-result-{task_id}.json",
            f"outputs/agent-offload-report-{task_id}.md",
        ],
    }
    artifacts = {
        f"outputs/agent-offload-result-{task_id}.json": json.dumps(result),
        f"outputs/agent-offload-report-{task_id}.md": "Finding: schema drift.\n",
    }
    marked: list[list[str]] = []
    monkeypatch.setattr(cli, "inspect_job", lambda _job_id: inspection)
    monkeypatch.setattr(cli, "artifact_text", lambda _job_id, path: artifacts[path])

    def fake_run_process(arguments, **_kwargs):
        marked.append(list(arguments))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli, "run_process", fake_run_process)
    retrieval = cli.retrieve("wj1")
    assert retrieval["report"] == "Finding: schema drift."
    assert retrieval["marked_processed"] is True
    assert marked == [["weft", "job", "mark-processed", "wj1"]]


def test_retrieve_preserves_a_worker_failure_artifact(monkeypatch) -> None:
    task_id = "task-2"
    result = completed_result(task_id, "unused")
    result.update(
        {
            "status": "preflight_failed",
            "harness_result": None,
            "harness_session_id": None,
            "report": None,
            "report_sha256": None,
            "failure": "worker source mismatch",
        }
    )
    monkeypatch.setattr(
        cli,
        "inspect_job",
        lambda _job_id: {
            "project": "agent-offload",
            "status": "completed",
            "produces": [
                f"outputs/agent-offload-result-{task_id}.json",
                f"outputs/agent-offload-report-{task_id}.md",
            ],
        },
    )
    monkeypatch.setattr(
        cli,
        "artifact_text",
        lambda _job_id, path: (
            json.dumps(result)
            if path.endswith(".json")
            else "# Agent offload failed\n\nworker source mismatch\n"
        ),
    )
    retrieval = cli.retrieve("wj2", mark_processed=False)
    assert retrieval["report"] is None
    assert retrieval["diagnostic_report"].endswith("worker source mismatch")


def test_retrieve_rejects_unbound_nested_failure_evidence(monkeypatch) -> None:
    task_id = "task-3"
    result = completed_result(task_id, "unused")
    result.update(
        {
            "status": "harness_failed",
            "report": None,
            "report_sha256": None,
            "failure": "agent harness failed",
        }
    )
    result["harness_result"]["prompt_sha256"] = "b" * 64
    monkeypatch.setattr(
        cli,
        "inspect_job",
        lambda _job_id: {
            "project": "agent-offload",
            "status": "completed",
            "produces": [
                f"outputs/agent-offload-result-{task_id}.json",
                f"outputs/agent-offload-report-{task_id}.md",
            ],
        },
    )
    monkeypatch.setattr(
        cli,
        "artifact_text",
        lambda _job_id, path: (
            json.dumps(result)
            if path.endswith(".json")
            else "# Agent offload failed\n\nagent harness failed\n"
        ),
    )
    with pytest.raises(cli.OffloadError, match="nested execution prompt digest"):
        cli.retrieve("wj3", mark_processed=False)


def test_retrieve_refuses_report_bytes_not_bound_to_envelope(monkeypatch) -> None:
    task_id = "task-1"
    result = completed_result(task_id, "Expected")
    monkeypatch.setattr(
        cli,
        "inspect_job",
        lambda _job_id: {
            "project": "agent-offload",
            "status": "completed",
            "produces": [
                f"outputs/agent-offload-result-{task_id}.json",
                f"outputs/agent-offload-report-{task_id}.md",
            ],
        },
    )
    monkeypatch.setattr(
        cli,
        "artifact_text",
        lambda _job_id, path: json.dumps(result) if path.endswith(".json") else "Different\n",
    )
    with pytest.raises(cli.OffloadError, match="does not match"):
        cli.retrieve("wj1")
