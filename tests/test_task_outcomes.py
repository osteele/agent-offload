"""Task lifecycle contracts with synthetic SDK evidence and a local Weft executable."""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from agent_execution.identity import source_sha256
from agent_execution.omp_execution import (
    OMP_EXECUTION_SCHEMA,
    OMP_SDK_VERSION,
    OMP_WRITE_TOOLS,
    omp_transcript,
)
from agent_execution.worker import WORKER_PROTOCOL_VERSION, HarnessOutcome, WorkerResult

from agent_offload import tasks
from agent_offload.protocol import package_source_sha256
from agent_offload.task_protocol import RESULT_SCHEMA, SCHEMA, digest, encode, manifest, result_path


@pytest.fixture
def task_factory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.py").write_text("before\n")
    (project / "hello.py").chmod(0o644)
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    replies = tmp_path / "replies.json"
    tools = tmp_path / "bin"
    tools.mkdir()
    weft = tools / "weft"
    weft.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        f"with open({str(tmp_path / 'weft-calls')!r}, 'a') as stream: stream.write(json.dumps(args) + '\\n')\n"
        "if args[0] == 'run':\n"
        "    key = args[args.index('--idempotency-key') + 1]\n"
        "    print(json.dumps({'api_version': 'weft.run.receipt.v1', 'job_id': 'wj-retry',\n"
        "        'placement_decision': 'accepted_immediately', 'accepted_immediately': True,\n"
        "        'selected_host': 'studio', 'source_pin': 'pin', 'idempotency_key': key}))\n"
        "    sys.exit(0)\n"
        f"with open({str(replies)!r}) as stream: data = json.load(stream)\n"
        "kind = {('job', 'inspect'): 'job', ('job', 'log'): 'log'}.get(tuple(args[:2]), 'result')\n"
        "entry = data[args[2]]\n"
        "if kind not in entry:\n"
        "    sys.exit(f'no {kind} for {args[2]}')\n"
        "print(entry[kind] if kind == 'log' else json.dumps(entry[kind]))\n"
    )
    weft.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    payloads = {}

    def create(identity, outcome="completed", after="after\n"):
        request = {
            "schema_version": SCHEMA,
            "id": identity,
            "brief": "Edit hello.py",
            "selector": "openai-codex/gpt-6-astra",
            "timeout": 30,
            "check": [
                sys.executable,
                "-c",
                f"raise SystemExit({7 if outcome == 'validation_failed' else 0})",
            ],
            "baseline": manifest(project, ["hello.py"]),
            "execution_sha256": source_sha256(),
            "offload_sha256": package_source_sha256(),
        }
        header = {
            "type": "execution",
            "schema_version": OMP_EXECUTION_SCHEMA,
            "sdk_version": OMP_SDK_VERSION,
            "harness": "omp",
            "selector": request["selector"],
            "tool_policy": "workspace-write-no-shell",
            "tools": list(OMP_WRITE_TOOLS),
            "session_id": "synthetic-task-session",
            "cwd": str(project),
            "prompt_sha256": digest(request["brief"].encode()),
            "execution_id": "00000000-0000-0000-0000-000000000001",
        }
        output = "\n".join(
            json.dumps(event)
            for event in [
                header,
                {"type": "session", "version": 3, "id": header["session_id"], "cwd": str(project)},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "provider": "openai-codex",
                        "model": "gpt-6-astra",
                        "stopReason": "stop",
                        "content": [{"type": "text", "text": "Synthetic writer response"}],
                    },
                },
                {"type": "agent_end", "messages": []},
                {"type": "execution_end", "session_id": header["session_id"]},
            ]
        )
        worker = WorkerResult(
            model_call_id=identity,
            provider="omp",
            status="completed",
            worker_version="test",
            worker_protocol_version=WORKER_PROTOCOL_VERSION,
            worker_source_sha256=request["execution_sha256"],
            ctx_version="",
            worker_cwd=str(project),
            started_at=1,
            completed_at=2,
            model_call_started=True,
            session_id=header["session_id"],
            harness=HarnessOutcome(0, output, ""),
            prompt_sha256=header["prompt_sha256"],
            omp_evidence=omp_transcript(output),
        )
        record = {
            "schema_version": SCHEMA,
            "id": identity,
            "project": str(project),
            "host": "studio",
            "title": request["brief"],
            "status": "queued",
            "job_id": f"wj{len(payloads) + 1}",
            "request": request,
            "request_sha256": digest(json.dumps(request).encode()),
        }
        result = {
            "schema_version": RESULT_SCHEMA,
            "id": identity,
            "status": outcome,
            "request_sha256": record["request_sha256"],
            "offload_sha256": request["offload_sha256"],
            "worker": worker.to_dict() if outcome != "failed" else None,
            "report": "Synthetic task result",
            "changes": []
            if outcome == "failed"
            else [
                {
                    "path": "hello.py",
                    "before": encode(b"before\n"),
                    "after": encode(after.encode()),
                    "before_mode": 0o644,
                    "after_mode": 0o644,
                }
            ],
            "validation": None
            if outcome == "failed"
            else {
                "command": request["check"],
                "exit_code": 7 if outcome == "validation_failed" else 0,
                "stdout": "",
                "stderr": "",
            },
        }
        with tasks.locked(identity) as directory:
            tasks.save(directory / "task.json", record)
            (directory / "request.json").write_text(json.dumps(request))
        payloads[record["job_id"]] = {
            "job": {"id": record["job_id"], "status": "completed"},
            "result": result,
        }
        replies.write_text(json.dumps(payloads))
        return record, result

    return create


@pytest.mark.parametrize("outcome", ["completed", "failed", "validation_failed"])
def test_monitoring_uses_validated_task_outcome(task_factory, capsys, outcome):
    record, _ = task_factory("monitored", outcome)
    assert tasks.handler(argparse.Namespace(action="status", id=record["id"])) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["status"] == outcome
    assert row["job_status"] == "completed"
    assert tasks.handler(argparse.Namespace(action="list")) == 0
    rows = json.loads(capsys.readouterr().out)["tasks"]
    assert rows[0]["status"] == outcome
    assert rows[0]["job_status"] == "completed"


def test_unverifiable_completion_does_not_hide_healthy_tasks(task_factory, tmp_path, capsys):
    broken, _ = task_factory("broken")
    task_factory("healthy")
    replies = tmp_path / "replies.json"
    payloads = json.loads(replies.read_text())
    payloads[broken["job_id"]]["result"]["request_sha256"] = "wrong-dispatch"
    replies.write_text(json.dumps(payloads))
    assert tasks.handler(argparse.Namespace(action="list")) == 0
    rows = {row["id"]: row for row in json.loads(capsys.readouterr().out)["tasks"]}
    assert rows["broken"]["status"] == "unknown"
    assert rows["broken"]["job_status"] == "completed"
    assert "retained dispatch" in rows["broken"]["error"]
    assert rows["healthy"]["status"] == "completed"


def unpublished_failure(tmp_path, record, *, job_status="failed", artifacts="unknown"):
    """Make the record's job end without a published result, as a worker refused at startup."""
    replies = tmp_path / "replies.json"
    payloads = json.loads(replies.read_text())
    payloads[record["job_id"]] = {
        "job": {
            "id": record["job_id"],
            "status": job_status,
            "exit_code": 1,
            "publication": {"required_artifacts_state": artifacts},
        },
        "log": "agent-offload-task-worker: installed source digest does not match",
    }
    replies.write_text(json.dumps(payloads))


def test_failed_job_without_result_reports_definite_status(task_factory, tmp_path, capsys):
    record, _ = task_factory("refused")
    unpublished_failure(tmp_path, record)
    assert tasks.handler(argparse.Namespace(action="status", id=record["id"])) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["status"] == "failed_without_result"
    assert row["job_status"] == "failed"
    assert "without publishing a task result" in row["error"]
    assert "installed source digest does not match" in row["error"]
    assert f"agent-offload task resume {record['id']}" in row["error"]
    assert "submit a new task instead" in row["error"]


@pytest.mark.parametrize(
    "job_status,artifacts",
    [("completed", "unknown"), ("failed", "ready")],
    ids=["completed", "published"],
)
def test_missing_result_of_publishing_job_stays_unknown(
    task_factory, tmp_path, capsys, job_status, artifacts
):
    record, _ = task_factory("unreadable")
    unpublished_failure(tmp_path, record, job_status=job_status, artifacts=artifacts)
    assert tasks.handler(argparse.Namespace(action="status", id=record["id"])) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["status"] == "unknown"
    assert "no result" in row["error"]


def test_resume_resubmits_failed_job_under_new_attempt_key(task_factory, tmp_path, capsys):
    record, _ = task_factory("retry")
    unpublished_failure(tmp_path, record)
    assert tasks.handler(argparse.Namespace(action="resume", id=record["id"])) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["job_id"] == "wj-retry"
    assert row["status"] == "queued"
    assert row["prior_job_ids"] == [record["job_id"]]
    retained = tasks.read_record(record["id"])
    assert retained["attempt"] == 2
    calls = [json.loads(line) for line in (tmp_path / "weft-calls").read_text().splitlines()]
    submitted = next(call for call in calls if call[0] == "run")
    assert submitted[submitted.index("--idempotency-key") + 1] == "retry-2"
    assert submitted[submitted.index("--output") + 1] == result_path("retry")


def change_retained_digest(record, field):
    record["request"][field] = "0" * 64
    directory = tasks.state_root() / record["id"]
    payload = directory / "request.json"
    tasks.save(payload, record["request"])
    record["request_sha256"] = digest(payload.read_bytes())
    tasks.save(directory / "task.json", record)


@pytest.mark.parametrize(
    "fields,names",
    [
        (("offload_sha256",), ("agent-offload",)),
        (("execution_sha256",), ("agent-execution",)),
        (("offload_sha256", "execution_sha256"), ("agent-offload", "agent-execution")),
    ],
)
def test_resume_refuses_failed_job_when_submitter_changed(
    task_factory, tmp_path, capsys, fields, names
):
    record, _ = task_factory("stale-submitter")
    unpublished_failure(tmp_path, record)
    for field in fields:
        change_retained_digest(record, field)

    assert tasks.handler(argparse.Namespace(action="resume", id=record["id"])) == 1
    error = capsys.readouterr().err
    for name, field in zip(names, fields, strict=True):
        current = package_source_sha256() if field == "offload_sha256" else source_sha256()
        assert f"{name} digest changed (retained {'0' * 12}, current {current[:12]})" in error
    assert "a retained request cannot be resumed after the submitting code changed" in error
    assert "Submit a new task for the same brief" in error
    retained = tasks.read_record(record["id"])
    assert retained == record
    assert retained["job_id"] == record["job_id"]
    assert retained.get("attempt") == record.get("attempt")
    assert retained.get("prior_job_ids") == record.get("prior_job_ids")
    calls = [json.loads(line) for line in (tmp_path / "weft-calls").read_text().splitlines()]
    assert not any(call[0] == "run" for call in calls)


def test_resume_unresolved_submission_when_submitter_changed(task_factory, tmp_path, capsys):
    record, _ = task_factory("unresolved-submitter")
    record.update(job_id=None, status="submission_unknown")
    change_retained_digest(record, "offload_sha256")

    assert tasks.handler(argparse.Namespace(action="resume", id=record["id"])) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["job_id"] == "wj-retry"
    assert row["status"] == "queued"
    retained = tasks.read_record(record["id"])
    assert retained["job_id"] == "wj-retry"
    assert retained.get("attempt", 1) == 1
    calls = [json.loads(line) for line in (tmp_path / "weft-calls").read_text().splitlines()]
    submitted = next(call for call in calls if call[0] == "run")
    assert submitted[submitted.index("--idempotency-key") + 1] == record["id"]


def test_resume_refuses_failed_job_when_baseline_changed(task_factory, tmp_path, capsys):
    record, _ = task_factory("drifted")
    unpublished_failure(tmp_path, record)
    (Path(record["project"]) / "hello.py").write_text("edited locally\n")
    assert tasks.handler(argparse.Namespace(action="resume", id=record["id"])) == 1
    assert "project changed" in capsys.readouterr().err
    retained = tasks.read_record(record["id"])
    assert retained["job_id"] == record["job_id"]
    assert "attempt" not in retained
    assert not any(
        json.loads(line)[0] == "run" for line in (tmp_path / "weft-calls").read_text().splitlines()
    )


def test_task_applications_cannot_overwrite_each_other(task_factory, monkeypatch):
    first, _ = task_factory("first", after="first\n")
    second, _ = task_factory("second", after="second\n")
    target = Path(first["project"]) / "hello.py"
    preflight_done, release_write = Event(), Event()
    replace = Path.replace

    def pause_first_write(path, destination):
        if Path(destination) == target and path.read_bytes() == b"first\n":
            preflight_done.set()
            assert release_write.wait(10), "concurrent application did not finish"
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", pause_first_write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(tasks.handler, argparse.Namespace(action="apply", id=first["id"]))
        try:
            assert preflight_done.wait(10), "first application never reached file replacement"
            second_exit = tasks.handler(argparse.Namespace(action="apply", id=second["id"]))
        finally:
            release_write.set()
        assert running.result(timeout=10) == 0
    assert second_exit == 1
    assert target.read_text() == "first\n"
    assert tasks.read_record(first["id"])["status"] == "applied"
    assert tasks.read_record(second["id"])["status"] == "queued"
    # Once the lock is free, the other task must detect the changed baseline.
    assert tasks.handler(argparse.Namespace(action="apply", id=second["id"])) == 1
    assert target.read_text() == "first\n"


@pytest.mark.parametrize("timed_out", [False, True], ids=["exit-failure", "timeout"])
def test_failed_check_publishes_result_and_exits_nonzero(
    task_factory, tmp_path, monkeypatch, timed_out
):
    from agent_offload import task_worker

    record, synthetic = task_factory("failed-check", "validation_failed")
    if timed_out:
        record["request"]["timeout"] = 1
        record["request"]["check"] = [
            sys.executable,
            "-c",
            "import time; print('validation started', flush=True); time.sleep(10)",
        ]
    record["request_sha256"] = digest(json.dumps(record["request"]).encode())
    tasks.save(tasks.state_root() / record["id"] / "task.json", record)
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "coding-task").write_text(json.dumps(record["request"]))
    monkeypatch.setenv("WEFT_PAYLOAD_DIR", str(payload))
    monkeypatch.chdir(record["project"])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            "--request-sha256",
            record["request_sha256"],
            "--task-id",
            record["id"],
        ],
    )
    # Only LLM generation is replaced; the check executes and publication is real.
    monkeypatch.setattr(
        task_worker,
        "execute_worker",
        lambda **kwargs: WorkerResult.parse(json.dumps(synthetic["worker"])),
    )
    with pytest.raises(SystemExit) as failure:
        task_worker.main()
    assert failure.value.code == 1
    result = json.loads(Path(result_path(record["id"])).read_text())
    assert result["status"] == "validation_failed"
    if timed_out:
        assert result["validation"]["exit_code"] is None
        assert "timed out" in result["validation"]["error"]
        assert "validation started" in result["validation"]["stdout"]
    else:
        assert result["validation"]["exit_code"] == 7
    replies = tmp_path / "replies.json"
    payloads = json.loads(replies.read_text())
    payloads[record["job_id"]]["result"] = result
    replies.write_text(json.dumps(payloads))
    assert tasks.retrieve(record)["status"] == "validation_failed"
    assert tasks.handler(argparse.Namespace(action="apply", id=record["id"])) == 1
    assert Path("hello.py").read_text() == "before\n"


def test_worker_preserves_real_sdk_preflight_failure(tmp_path):
    from agent_offload.task_worker import execute

    (tmp_path / "source.txt").write_text("unchanged\n")
    result = execute(
        {
            "id": "sdk-contract",
            "brief": "Do not launch a model",
            "selector": "openai-codex/gpt-6-astra",
            "timeout": 1,
            "check": [],
            "baseline": manifest(tmp_path, ["source.txt"]),
            "execution_sha256": "0" * 64,
            "request_sha256": "0" * 64,
        },
        tmp_path,
    )
    assert result["status"] == "failed"
    assert result["worker"]["status"] == "preflight_failed"
    assert result["worker"]["model_call_started"] is False
    evidence = tmp_path / ".agent-execution/results/sdk-contract.json"
    assert json.loads(evidence.read_text())["status"] == "preflight_failed"
