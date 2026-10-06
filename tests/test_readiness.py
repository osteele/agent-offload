from __future__ import annotations

import json
import subprocess

import pytest
from agent_execution.omp_execution import OMP_WRITER_SELECTORS
from agent_execution.worker import installed_worker_identity

from agent_offload import readiness
from agent_offload.task_protocol import TaskError

SELECTOR = min(OMP_WRITER_SELECTORS)
IDENTITY = installed_worker_identity().to_dict()
SCHEMA = "agent-execution.writer-readiness/v1"


def response(rows: list[dict[str, object]], *, schema: str = SCHEMA, identity=None) -> str:
    return json.dumps(
        {
            "schema_version": schema,
            "worker_identity": IDENTITY if identity is None else identity,
            "writers": rows,
        }
    )


def row(
    selector: str = SELECTOR,
    *,
    model_available: bool = True,
    credential_available: bool = True,
    available: bool = True,
    detail: str = "ready",
) -> dict[str, object]:
    return {
        "selector": selector,
        "model_available": model_available,
        "credential_available": credential_available,
        "available": available,
        "detail": detail,
    }


def install_response(monkeypatch, text: str, *, returncode: int = 0, stderr: str = ""):
    calls: list[dict[str, object]] = []

    def run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, returncode, stdout=text, stderr=stderr)

    monkeypatch.setattr(readiness.subprocess, "run", run)
    return calls


@pytest.mark.parametrize(
    "payload",
    [
        response([row()], schema="wrong"),
        response([row("other/selector")]),
        response([row(), row()]),
        response([]),
        response([row(available=1)]),
        response([row(model_available=False, available=True)]),
    ],
)
def test_probe_rejects_malformed_or_mismatched_rows(monkeypatch, payload):
    install_response(monkeypatch, payload)
    with pytest.raises(TaskError):
        readiness.probe_writers(selectors=[SELECTOR])


def test_probe_rejects_pinned_execution_identity_mismatch(monkeypatch):
    identity = dict(IDENTITY)
    identity["source_sha256"] = "0" * 64
    install_response(monkeypatch, response([row()], identity=identity))
    with pytest.raises(TaskError, match="identity"):
        readiness.probe_writers(selectors=[SELECTOR], execution_sha256="1" * 64)


def test_probe_reports_ssh_failure(monkeypatch):
    calls = install_response(monkeypatch, "", returncode=255, stderr="connection refused")
    with pytest.raises(TaskError, match="connection refused"):
        readiness.probe_writers(selectors=[SELECTOR])
    assert calls


@pytest.mark.parametrize(
    "host,account,target",
    [
        ("studio", "worker", "worker@studio"),
        ("agent@studio", "worker", "agent@studio"),
    ],
)
def test_probe_uses_requested_remote_target(monkeypatch, host, account, target):
    calls = install_response(monkeypatch, response([row()]))
    readiness.probe_writers(host=host, account=account, selectors=[SELECTOR])
    argv = calls[0]["argv"]
    assert target in argv
    remote_command = " ".join(str(argument) for argument in argv)
    assert "agent-offload-worker" in remote_command
    assert "probe-writers" in remote_command


def test_probe_rejects_unadmitted_selector_before_ssh(monkeypatch):
    calls = install_response(monkeypatch, response([]))
    with pytest.raises(TaskError, match="selector"):
        readiness.probe_writers(selectors=["not/admitted"])
    assert calls == []
