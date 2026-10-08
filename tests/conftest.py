from __future__ import annotations

import os
from pathlib import Path

import pytest

POSIX = os.name == "posix"
if POSIX:
    from agent_offload import tasks

    REAL_ENSURE_WORKER = tasks.ensure_worker
else:
    # agent-offload and agent-execution lock state with fcntl and manage POSIX
    # process groups; elsewhere only the entry points' refusal is tested.
    collect_ignore = [
        path.name
        for path in Path(__file__).parent.glob("test_*.py")
        if path.name != "test_install_guard.py"
    ]
PINNED_BUILD = "b" * 64
PINNED_BIN = f"/Users/agent/.local/state/agent-host-sync/builds/agent-offload/{PINNED_BUILD}/bin"


def status_envelope(selectors, *, overrides=None):
    """Synthetic upstream observations for isolated consumer-policy tests."""
    from agent_execution.worker import installed_worker_identity

    identity = installed_worker_identity().to_dict()
    rows = []
    for selector in selectors:
        facts = {}
        for name in ("capability", "authentication", "quota", "generation", "transport"):
            known = name in {"capability", "authentication", "transport"}
            facts[name] = {
                "state": "available" if known else "unknown",
                "observed_at": "2026-10-08T12:00:00Z" if known else None,
                "expires_at": "2026-10-08T12:05:00Z" if known else None,
                "age_seconds": 12.0 if known else None,
                "stale": not known,
                "source": {"tool": "agent-execution", "method": "synthetic-fixture"},
                "detail": "synthetic observation" if known else "unobserved",
            }
        for name, change in (overrides or {}).items():
            facts[name].update(change)
        route = selector.split("/", 1)[0]
        rows.append(
            {
                "subject": {
                    "harness": "omp",
                    "surface": "offload-task",
                    "selector": selector,
                    "tool_policy": "workspace-write-no-shell",
                    "route": route,
                    "billing_pool": route,
                    "host": "studio",
                    "os_user": "agent",
                    "transport": "weft",
                    "requester_host": "laptop",
                    "requester_user": "operator",
                    "execution_sha256": identity["source_sha256"],
                    "credential_fingerprint": "host:studio:unknown",
                    "fingerprint_scope": "host",
                    "profile": None,
                    "environment_fingerprint": "0" * 64,
                },
                "facts": facts,
                "credential_basis": None,
            }
        )
    return {
        "schema_version": "agent-execution.execution-status/v1",
        "generated_at": "2026-10-08T12:00:12Z",
        "worker_identity": identity,
        "observer": {"host": "studio", "os_user": "agent"},
        "rows": rows,
        "diagnostics": [],
    }


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Keep the developer's own config out of tests; tests that need one write it here."""
    path = tmp_path / "agent-offload-config.toml"
    monkeypatch.setenv("AGENT_OFFLOAD_CONFIG", str(path))
    return path


@pytest.fixture
def agent_host_sync_provider(isolated_config):
    isolated_config.write_text('[worker]\nprovider = "agent-host-sync"\n')


@pytest.fixture(autouse=True)
def pinned_worker(monkeypatch):
    """Isolate provisioning and remote credential checks from task tests."""
    if not POSIX:
        return

    def ensure(record: dict) -> None:
        record["bin_dir"] = PINNED_BIN
        record["build_id"] = PINNED_BUILD

    monkeypatch.setattr(tasks, "ensure_worker", ensure)
    monkeypatch.setattr(tasks, "require_writer", lambda record: None)


@pytest.fixture(autouse=True)
def pinned_local_probe(monkeypatch):
    """Avoid real credentials; launch-refusal tests override this observation."""
    if not POSIX:
        return
    from agent_execution import execution_status

    monkeypatch.setattr(
        execution_status,
        "probe_status",
        lambda **kwargs: status_envelope(kwargs["selectors"]),
    )
