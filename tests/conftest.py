from __future__ import annotations

import pytest

from agent_offload import tasks

REAL_ENSURE_WORKER = tasks.ensure_worker
PINNED_BUILD = "b" * 64
PINNED_BIN = f"/Users/agent/.local/state/agent-host-sync/builds/agent-offload/{PINNED_BUILD}/bin"


@pytest.fixture(autouse=True)
def pinned_worker(monkeypatch):
    """Submission provisions its worker through agent-host-sync over SSH.

    Tests stand in an already provisioned build rather than reach Studio;
    test_tasks.py exercises the real function against a faked command.
    """

    def ensure(record: dict) -> None:
        record["bin_dir"] = PINNED_BIN
        record["build_id"] = PINNED_BUILD

    monkeypatch.setattr(tasks, "ensure_worker", ensure)
