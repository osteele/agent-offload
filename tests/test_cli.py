from __future__ import annotations

import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_execution.worker import WorkerIdentity

from agent_offload import cli
from agent_offload.protocol import (
    RESULT_SCHEMA,
    installed_build_id,
    sha256_text,
    worker_identity,
)


def completed_result(task_id: str, report: str) -> dict[str, object]:
    prompt_sha256 = "a" * 64
    execution_identity = WorkerIdentity(
        package_version="0.1.0", protocol_version=1, source_sha256="d" * 64
    )
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
                "import_receipt": {"schema_version": 2, "outcome": "success"},
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


def queued_receipt(**fields: object) -> str:
    return json.dumps(
        {
            "api_version": "weft.run.receipt.v1",
            "job_id": "wj1",
            "placement_decision": "queued",
            "selected_host": "studio",
            "source_pin": "sha256:abc",
            "accepted_immediately": False,
            "deduplicated": False,
            "idempotency_key": "task-1",
            **fields,
        }
    )


def test_receipt_accepts_a_job_left_to_the_weft_daemon() -> None:
    receipt = cli.WeftReceipt.parse(queued_receipt(), idempotency_key="task-1")

    assert (receipt.job_id, receipt.placement_decision) == ("wj1", "queued")


@pytest.mark.parametrize(
    "fields", [{"accepted_immediately": True}, {"deduplicated": True}, {"job_id": ""}]
)
def test_receipt_rejects_a_queued_receipt_that_contradicts_itself(fields: dict) -> None:
    with pytest.raises(ValueError):
        cli.WeftReceipt.parse(queued_receipt(**fields), idempotency_key="task-1")


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


def test_doctor_refuses_retired_native_codex_without_probe(monkeypatch) -> None:
    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("retired provider must not be probed")

    monkeypatch.setattr(cli, "probe_status", unexpected_probe)
    result = cli.doctor(host="studio", provider="codex")
    assert result["schema_version"] == "agent-offload.doctor/v2"
    assert result["execution_status"] is None
    assert result["admission"]["allowed"] is False
    assert "native Codex execution is retired" in result["admission"]["reasons"][0]
    assert "available" not in result


def test_submit_refuses_retired_native_codex_before_weft(monkeypatch, tmp_path: Path) -> None:
    brief = tmp_path / "brief.txt"
    brief.write_text("Inspect the project.\n")

    def unexpected_submission(*_args, **_kwargs):
        raise AssertionError("retired provider must not reach Weft")

    monkeypatch.setattr(cli, "run_process", unexpected_submission)
    with pytest.raises(cli.OffloadError, match="native Codex execution is retired"):
        cli.submit(
            project=tmp_path,
            brief_path=brief,
            host="studio",
            selector="gpt-5.5",
            effort="medium",
            harness_timeout=10,
            ctx_timeout=5,
        )


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
    retrieval = cli.retrieve("wj1", expected_execution_source_sha256="d" * 64)
    assert retrieval["report"] == "Finding: schema drift."
    assert retrieval["marked_processed"] is True
    assert marked == [["weft", "job", "mark-processed", "wj1"]]


def test_retrieve_refuses_missing_historical_nested_source_baseline(monkeypatch) -> None:
    task_id = "task-1"
    result = completed_result(task_id, "Historical report")
    result_artifact = f"outputs/agent-offload-result-{task_id}.json"
    report_artifact = f"outputs/agent-offload-report-{task_id}.md"
    monkeypatch.setattr(
        cli,
        "inspect_job",
        lambda _job_id: {
            "status": "completed",
            "project": "agent-offload",
            "produces": [result_artifact, report_artifact],
        },
    )
    monkeypatch.setattr(cli, "artifact_text", lambda *_args: json.dumps(result))

    def unexpected_mark(*_args, **_kwargs):
        raise AssertionError("unverifiable evidence must not mark the job processed")

    monkeypatch.setattr(cli, "run_process", unexpected_mark)
    with pytest.raises(cli.OffloadError, match="did not retain a nested worker source baseline"):
        cli.retrieve("wj1")


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
        cli.retrieve("wj1", expected_execution_source_sha256="d" * 64)


@pytest.mark.parametrize("available,expected_exit", [(True, 0), (False, 1)])
def test_doctor_cli_exit_tracks_active_writer_readiness(
    monkeypatch, capsys, available: bool, expected_exit: int
) -> None:
    from agent_execution.omp_execution import OMP_WRITER_SELECTORS

    selector = min(OMP_WRITER_SELECTORS)
    state = "available" if available else "unavailable"
    from conftest import status_envelope

    status = status_envelope([selector], overrides={"capability": {"state": state}})
    monkeypatch.setattr(cli, "probe_status", lambda **_kwargs: status)
    with pytest.raises(SystemExit) as exit_code:
        cli.main(["doctor", "--host", "studio", "--selector", selector])
    assert exit_code.value.code == expected_exit
    output = capsys.readouterr().out
    assert ("admit:" if available else "refuse:") in output


def _answer_requested_writers(monkeypatch) -> list[list[str]]:
    """Fake Execution's fresh exact-selector observation."""
    probed: list[list[str]] = []

    def probe(**kwargs):
        selectors = kwargs["selectors"]
        probed.append(selectors)
        from conftest import status_envelope

        return status_envelope(selectors)

    monkeypatch.setattr(cli, "probe_status", probe)
    return probed


def _openai_writers() -> list[str]:
    from agent_execution.omp_execution import OMP_WRITER_SELECTORS

    return sorted(item for item in OMP_WRITER_SELECTORS if item.startswith("openai-codex/"))


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ([], lambda: [cli.DEFAULT_SELECTOR]),
        (["--provider", "openai-codex"], _openai_writers),
        (
            ["--provider", "openai-codex", "--selector", cli.DEFAULT_SELECTOR],
            lambda: [cli.DEFAULT_SELECTOR],
        ),
    ],
    ids=["bare-default-only", "provider-all-admitted", "selector-overrides-provider"],
)
def test_doctor_selects_writers_to_probe(monkeypatch, capsys, arguments, expected) -> None:
    probed = _answer_requested_writers(monkeypatch)
    with pytest.raises(SystemExit) as exit_code:
        cli.main(["doctor", "--host", "studio", "--json", *arguments])
    assert exit_code.value.code == 0
    assert probed == [expected()]
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "agent-offload.doctor/v2"
    assert report["admission"]["allowed"] is True
    assert (
        sorted(row["subject"]["selector"] for row in report["execution_status"]["rows"])
        == expected()
    )


def _installed_layout(root: Path, build_id: str, *, marker: str | None) -> Path:
    """Mirror an installed build's directory depth under a build store."""
    build = root / "builds/agent-offload" / build_id
    module = build / "tools/agent-offload/lib/python3.11/site-packages/agent_offload"
    module.mkdir(parents=True)
    if marker is not None:
        (build / "BUILD_ID").write_text(marker + "\n")
    return module / "protocol.py"


def test_build_id_reports_the_build_the_package_runs_from(tmp_path: Path) -> None:
    build_id = "a" * 64
    origin = _installed_layout(tmp_path, build_id, marker=build_id)
    assert installed_build_id(origin) == build_id


def test_build_id_ignores_a_marker_naming_another_build(tmp_path: Path) -> None:
    # A marker whose content is not its own directory name is not this build's
    # marker, so a stray or copied file cannot forge a build identity.
    origin = _installed_layout(tmp_path, "b" * 64, marker="c" * 64)
    assert installed_build_id(origin) is None


def test_build_id_is_absent_for_a_source_checkout(tmp_path: Path) -> None:
    origin = _installed_layout(tmp_path, "d" * 64, marker=None)
    assert installed_build_id(origin) is None
    assert worker_identity()["build_id"] is None
