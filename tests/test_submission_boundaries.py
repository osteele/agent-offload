"""Regressions for reused worker identity and durable provisioning phases."""

import pytest
from agent_execution.identity import source_sha256

from agent_offload import tasks, worker_build
from agent_offload.protocol import package_source_sha256
from agent_offload.task_protocol import TaskError


@pytest.mark.parametrize("wrong_side", ["offload", "execution"])
def test_worker_build_rejects_reused_build_id_with_wrong_source_digest(monkeypatch, wrong_side):
    offload = package_source_sha256()
    execution = source_sha256()
    returned_offload = "1" * 64 if wrong_side == "offload" else offload
    returned_execution = "2" * 64 if wrong_side == "execution" else execution
    build_id = worker_build.ssh_build_id(offload, execution)
    monkeypatch.setattr(
        worker_build,
        "ensure_over_ssh",
        lambda *args: worker_build.WorkerBuild(
            build_id, "/worker/bin", returned_offload, returned_execution
        ),
    )

    with pytest.raises(TaskError, match=f"agent-{wrong_side} digest differs"):
        worker_build.ensure_worker_build(
            provider="ssh",
            host="box",
            account="agent",
            offload_sha256=offload,
            execution_sha256=execution,
        )


@pytest.mark.parametrize("phase", ["in_progress", "failed"])
def test_submission_view_honors_reprovision_phase_despite_retained_build_pin(phase):
    view = tasks.submission_view(
        {
            "status": "submitting",
            "job_id": None,
            "bin_dir": "/worker/bin",
            "build_id": "retained-build",
            "provisioning": phase,
            "provisioning_error": "remote detail" if phase == "failed" else None,
        }
    )

    assert view["provisioning"] == phase
    assert view["stage"] == f"provisioning_{phase}"
    if phase == "failed":
        assert view["provisioning_error"] == "remote detail"
