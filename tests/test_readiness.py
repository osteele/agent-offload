from __future__ import annotations

import pytest
from conftest import status_envelope

from agent_offload import readiness
from agent_offload.task_protocol import TaskError

SELECTOR = "openai-codex/gpt-6-luna"


def test_transport_failure_is_an_explicit_task_error(monkeypatch):
    def failed(**kwargs):
        raise ValueError("connection refused")

    monkeypatch.setattr(readiness, "probe_remote_status", failed)
    with pytest.raises(TaskError, match="connection refused"):
        readiness.probe_status(host="studio", selectors=[SELECTOR])


@pytest.mark.parametrize("fact", ["capability", "authentication", "transport"])
@pytest.mark.parametrize(
    "state,stale", [("available", True), ("unknown", False), ("unavailable", False)]
)
def test_task_admission_requires_current_supported_authenticated_transport(fact, state, stale):
    status = status_envelope([SELECTOR], overrides={fact: {"state": state, "stale": stale}})
    decision = readiness.admission(status, [SELECTOR])
    assert not decision["allowed"]
    assert any(fact in reason for reason in decision["reasons"])


@pytest.mark.parametrize(
    "state,stale,allowed",
    [
        ("unknown", True, True),
        ("unavailable", True, True),
        ("unavailable", False, False),
    ],
)
def test_only_a_current_quota_refusal_blocks_admission(state, stale, allowed):
    status = status_envelope([SELECTOR], overrides={"quota": {"state": state, "stale": stale}})
    assert readiness.admission(status, [SELECTOR])["allowed"] is allowed


def test_generation_refusal_does_not_overwrite_current_authentication():
    status = status_envelope(
        [SELECTOR],
        overrides={
            "generation": {
                "state": "unavailable",
                "stale": False,
                "detail": "previous provider refusal",
            },
        },
    )
    assert readiness.admission(status, [SELECTOR])["allowed"]


def test_every_selected_writer_must_have_an_observation():
    second = "anthropic/claude-opus-5-5"
    decision = readiness.admission(status_envelope([SELECTOR]), [SELECTOR, second])
    assert not decision["allowed"]
    assert any(second in reason for reason in decision["reasons"])
    assert not readiness.admission(status_envelope([]), [])["allowed"]
