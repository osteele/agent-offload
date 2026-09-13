"""Contract tests for the attached relay conductor.

The bridge is replaced by a scripted fake so the conductor's observable
contracts — versioned frames, ready-first sequencing, complete tool
lifecycle, sanctioned tool names, served-path confinement, request
binding, process-group termination on timeout, detach refusal — are
exercised without a remote host or live provider.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent_offload import cli
from agent_offload import relay as relay_module
from agent_offload.relay_protocol import RESULT_SCHEMA, RelayProtocolError

FAKE_BRIDGE = textwrap.dedent(
    """
    import hashlib
    import json, os, signal, sys, time

    scenario = os.environ["RELAY_FAKE_SCENARIO"]
    request = json.loads(sys.stdin.read())
    if os.environ.get("RELAY_FAKE_REQUEST_OUT"):
        with open(os.environ["RELAY_FAKE_REQUEST_OUT"], "w") as handle:
            json.dump(request, handle)

    def frame(payload):
        sys.stdout.write(json.dumps(payload) + "\\n")
        sys.stdout.flush()

    def terminate(signum, _handler):
        with open(os.environ["RELAY_FAKE_TERMINATED_MARKER"], "w") as handle:
            handle.write("terminated")
        sys.exit(130)

    signal.signal(signal.SIGTERM, terminate)

    if scenario == "sleep":
        time.sleep(120)
        sys.exit(0)

    if scenario == "garbage":
        sys.stdout.write("not json at all\\n")
        sys.stdout.flush()
        time.sleep(60)

    if scenario == "error":
        frame({"frame": "error", "schema_version": "agent-offload.relay-bridge/v1",
               "error": {"message": "server refused host tools"}})
        sys.exit(3)

    flags = ("--no-tools --no-lsp --no-extensions --no-skills --no-rules "
             "--no-session --no-pty")
    if scenario == "lookalike":
        flags = ("--no-tools=false --no-lsp --no-extensions --no-skills "
                 "--no-rules --no-session --no-pty")
    remote_argv = ("ssh agent@studio exec omp --mode rpc " + flags) if (
        scenario != "no_flags"
    ) else "ssh agent@studio exec omp --mode rpc"
    if scenario == "malformed_argv":
        remote_argv = "'"

    ready = {"frame": "ready", "schema_version": "agent-offload.relay-bridge/v1",
             "sdk_version": "0.0.0-test", "omp_version": "omp/v-test",
             "host_identity": "test-host", "remote_argv": remote_argv,
             "registered_tools": ["review_read", "review_glob", "review_grep"]}
    if scenario != "no_ready":
        frame(ready)
    if scenario == "duplicate_ready":
        frame(dict(ready))
    if scenario == "malformed_ready":
        frame({"frame": "ready", "schema_version": "agent-offload.relay-bridge/v1",
               "sdk_version": "0.0.0-test",
               "registered_tools": ["review_read", "review_glob", "review_grep"]})

    # The combined prompt must match the conductor's formulation exactly;
    # this digest is what the conductor binds its own computation against.
    prompt = request["prompt"]
    if request.get("system_prompt"):
        prompt = ("<project-context>\\n" + request["system_prompt"]
                  + "\\n</project-context>\\n\\n" + prompt)
    prompt_digest = hashlib.sha256(prompt.encode()).hexdigest()

    root = request["project_root"]
    served = "/etc/passwd" if scenario == "outside_root" else root + "/notes.txt"
    digest = "a" * 64
    events = []
    if scenario in ("happy", "outside_root"):
        events = [
            {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "review_read"},
            {"type": "tool_execution_end", "toolCallId": "c1",
             "result": {"content": [{"type": "text", "text": "data"}],
                        "details": {"served_paths": [
                            {"path": served, "sha256": digest, "bytes": 4,
                             "truncated": False}]},
                        "isError": False}},
        ]
    elif scenario == "unpaired":
        events = [
            {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "review_read"},
        ]
    elif scenario == "bad_tool":
        events = [
            {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash"},
            {"type": "tool_execution_end", "toolCallId": "c1",
             "result": {"content": [{"type": "text", "text": "did things"}],
                        "details": {}, "isError": False}},
        ]
    elif scenario == "evidence_substitution":
        events = [
            {"type": "tool_execution_start", "toolCallId": "c2", "toolName": "review_glob"},
            {"type": "tool_execution_end", "toolCallId": "c2",
             "result": {"content": [{"type": "text", "text": "substituted"}],
                        "details": {}, "isError": False}},
        ]

    for event in events:
        frame({"frame": "event", "schema_version": "agent-offload.relay-bridge/v1",
               "event": event})

    if scenario == "evidence_substitution":
        # Stream a different lifecycle than the one the result claims, so the
        # conductor's streamed-vs-result equality check is what fires.
        streamed = [
            {"type": "tool_execution_start", "toolCallId": "streamed-1",
             "toolName": "review_read"},
        ]
        for event in streamed:
            frame({"frame": "event", "schema_version": "agent-offload.relay-bridge/v1",
                   "event": event})

    registered = ["review_read", "review_glob", "review_grep"]
    if scenario == "registry_drift":
        registered = ["review_read"]
    model = {"provider": "anthropic", "id": "claude-opus-4-6"}
    if scenario == "model_drift":
        model = {"provider": "anthropic", "id": "claude-opus-4-5"}
    host_target = "agent@elsewhere" if scenario == "host_drift" else request["host_target"]
    bounds = {"maxReadBytes": 1} if scenario == "bounds_drift" else request["bounds"]
    result_digest = "c" * 64 if scenario == "prompt_drift" else prompt_digest

    result = {
        "sdk_version": "0.0.0-test", "omp_version": "omp/v-test",
        "host_target": host_target, "host_identity": "test-host",
        "remote_argv": remote_argv, "remote_scratch": "/tmp/scratch",
        "registered_tools": registered,
        "session_id": "sess-1", "session_file": None,
        "model": model,
        "started_at": 1.0, "ended_at": 2.0, "failure": None, "cancelled": False,
        "events": events, "final_response": "done",
        "stats": None,
        "prompt_sha256": result_digest, "bounds": bounds}
    if scenario == "missing_events":
        del result["events"]
    if scenario == "empty_success":
        result["final_response"] = ""
    if scenario == "bad_failure":
        result["failure"] = 7
    if scenario == "bad_cancelled":
        result["cancelled"] = "no"
    frame({"frame": "result", "schema_version": "agent-offload.relay-bridge/v1",
           "result": result})
    """
).strip()


@pytest.fixture
def relay_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "notes.txt").write_text("data")
    (root / "CLAUDE.md").write_text("# project instructions")
    monkeypatch.chdir(tmp_path)
    return root


@pytest.fixture
def fake_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake_bridge.py"
    script.write_text(FAKE_BRIDGE)
    monkeypatch.setattr(relay_module, "_ensure_runtime", lambda runtime_dir: script)
    monkeypatch.setattr(
        relay_module,
        "_bridge_command",
        lambda bun, bridge: [sys.executable, str(bridge)],
    )
    return script


def _relay_namespace(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "project": None,
        "prompt": "review this",
        "prompt_file": None,
        "host": "agent@studio",
        "timeout": 30.0,
        "json": False,
        "detach": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _marker(relay_project: Path, name: str) -> Path:
    return relay_project.parent / f"relay-fake-{name}"


def _run_scenario(
    relay_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    *,
    timeout_seconds: float = 20.0,
) -> tuple[Path, dict]:
    monkeypatch.setenv("RELAY_FAKE_SCENARIO", scenario)
    monkeypatch.setenv("RELAY_FAKE_REQUEST_OUT", str(_marker(relay_project, "request.json")))
    monkeypatch.setenv("RELAY_FAKE_TERMINATED_MARKER", str(_marker(relay_project, "terminated")))
    monkeypatch.setattr(relay_module, "RELAY_DEADLINE_GRACE_SECONDS", 0.5, raising=False)
    return relay_module.run_relay(
        project_root=relay_project,
        prompt="review this",
        host="agent@studio",
        timeout_seconds=timeout_seconds,
    )


def test_rejects_detach(relay_project: Path) -> None:
    assert relay_module.cmd_relay(_relay_namespace(detach=True, project=relay_project)) == 2


def test_requires_exactly_one_prompt_source(relay_project: Path) -> None:
    assert (
        relay_module.cmd_relay(
            _relay_namespace(project=relay_project, prompt=None, prompt_file=None)
        )
        == 2
    )
    both = relay_module.cmd_relay(
        _relay_namespace(project=relay_project, prompt="x", prompt_file=relay_project / "CLAUDE.md")
    )
    assert both == 2


def test_parser_rejects_generic_selector_overrides() -> None:
    """The grounded lane is pinned; generic override flags do not exist."""
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["relay", "--model", "zai/glm-5.3-flash"])
    with pytest.raises(SystemExit):
        parser.parse_args(["relay", "--provider", "openai"])


def test_happy_path_writes_validated_result(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_path, record = _run_scenario(relay_project, monkeypatch, "happy")
    assert record["schema_version"] == RESULT_SCHEMA
    assert record["tool_call_count"] == 1
    assert record["durability"] == "none-attached-live-local-tools"
    assert record["snapshot_equivalent"] is False
    assert record["model"] == {"provider": "anthropic", "id": "claude-opus-4-6"}
    served = record["tool_calls"][0]["served_paths"][0]
    assert served["path"] == str(relay_project / "notes.txt")
    assert result_path.exists()
    on_disk = json.loads(result_path.read_text())
    assert on_disk["schema_version"] == RESULT_SCHEMA
    assert record["bounds"]["maxReadBytes"] == 524288


def test_request_pins_grounded_selector(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RELAY_FAKE_SCENARIO", "happy")
    monkeypatch.setenv("RELAY_FAKE_REQUEST_OUT", str(_marker(relay_project, "request.json")))
    monkeypatch.setenv("RELAY_FAKE_TERMINATED_MARKER", str(_marker(relay_project, "terminated")))
    relay_module.run_relay(
        project_root=relay_project,
        prompt="review this",
        host="agent@studio",
        timeout_seconds=20.0,
    )
    request = json.loads(_marker(relay_project, "request.json").read_text())
    assert request["schema_version"] == "agent-offload.relay-request/v1"
    assert request["project_root"] == str(relay_project)
    assert request["provider"] == "anthropic"
    assert request["model"] == "anthropic/claude-opus-4-6"
    assert request["bounds"]["maxReadBytes"] > 0
    assert request["bounds"]["maxGrepMatches"] > 0
    assert request["bounds"]["maxToolArgsBytes"] > 0
    assert request["host_target"] == "agent@studio"
    assert request["system_prompt"] is not None
    assert "project instructions" in request["system_prompt"]


def test_refuses_unpaired_tool_call(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="without results"):
        _run_scenario(relay_project, monkeypatch, "unpaired")


def test_refuses_served_path_outside_root(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="outside root"):
        _run_scenario(relay_project, monkeypatch, "outside_root")


def test_refuses_garbage_stream(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="violated its schema"):
        _run_scenario(relay_project, monkeypatch, "garbage")


def test_refuses_streamed_evidence_substitution(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="differ from the streamed"):
        _run_scenario(relay_project, monkeypatch, "evidence_substitution")


def test_refuses_missing_ready_frame(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="before the ready frame"):
        _run_scenario(relay_project, monkeypatch, "no_ready")


def test_refuses_duplicate_ready_frame(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="duplicate ready frame"):
        _run_scenario(relay_project, monkeypatch, "duplicate_ready")


def test_refuses_malformed_ready_frame(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="omp_version missing or wrong type"):
        _run_scenario(relay_project, monkeypatch, "malformed_ready")


def test_refuses_unsanctioned_tool_name(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="unsanctioned relay tool"):
        _run_scenario(relay_project, monkeypatch, "bad_tool")


def test_refuses_registered_tools_drift(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="registered_tools must be exactly"):
        _run_scenario(relay_project, monkeypatch, "registry_drift")


def test_refuses_model_selector_drift(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="drifted from the pinned grounded selector"):
        _run_scenario(relay_project, monkeypatch, "model_drift")


def test_refuses_host_target_drift(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="host_target drift"):
        _run_scenario(relay_project, monkeypatch, "host_drift")


def test_refuses_bounds_drift(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="bounds drift"):
        _run_scenario(relay_project, monkeypatch, "bounds_drift")


def test_refuses_prompt_digest_drift(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="prompt digest does not match"):
        _run_scenario(relay_project, monkeypatch, "prompt_drift")


def test_refuses_missing_result_field_before_access(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="missing keys: 'events'"):
        _run_scenario(relay_project, monkeypatch, "missing_events")


def test_refuses_malformed_remote_argv(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="not valid shell syntax"):
        _run_scenario(relay_project, monkeypatch, "malformed_argv")


def test_terminate_group_falls_back_when_group_signal_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )

    def deny_group_signal(_pid: int, _signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(relay_module.os, "killpg", deny_group_signal)
    try:
        relay_module._terminate_group(process)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert process.poll() is not None


def test_refuses_empty_success_response(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="requires a non-empty assistant response"):
        _run_scenario(relay_project, monkeypatch, "empty_success")


@pytest.mark.parametrize("scenario", ["bad_failure", "bad_cancelled"])
def test_refuses_invalid_result_status_types(
    relay_project: Path,
    fake_bridge: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    with pytest.raises(RelayProtocolError, match=r"must be a (string or null|boolean)"):
        _run_scenario(relay_project, monkeypatch, scenario)


def test_refuses_error_frame(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(relay_module.RelayError, match="failed closed"):
        _run_scenario(relay_project, monkeypatch, "error")


def test_timeout_terminates_bridge_process_group(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _marker(relay_project, "terminated")
    monkeypatch.setenv("RELAY_FAKE_SCENARIO", "sleep")
    monkeypatch.setenv("RELAY_FAKE_TERMINATED_MARKER", str(marker))
    monkeypatch.setattr(relay_module, "RELAY_DEADLINE_GRACE_SECONDS", 0.3, raising=False)
    with pytest.raises(relay_module.RelayError, match="exceeded"):
        relay_module.run_relay(
            project_root=relay_project,
            prompt="review this",
            host="agent@studio",
            timeout_seconds=0.5,
        )
    assert marker.read_text() == "terminated"
