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
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agent_offload import cli
from agent_offload import relay as relay_module
from agent_offload.relay_protocol import RESULT_SCHEMA, RelayProtocolError

FAKE_BRIDGE = textwrap.dedent(
    """
    import hashlib
    import json, os, select, signal, sys, time

    scenario = os.environ["RELAY_FAKE_SCENARIO"]
    request_line = sys.stdin.readline()
    request = json.loads(request_line)
    if scenario == "lifeline":
        with open(os.environ["RELAY_FAKE_STARTED_MARKER"], "w") as handle:
            handle.write("started")
        while sys.stdin.read(1):
            pass
        with open(os.environ["RELAY_FAKE_EOF_MARKER"], "w") as handle:
            handle.write("eof")
        sys.exit(0)
    if scenario == "stdin_open":
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        is_open = not readable or sys.stdin.read(1) != ""
        with open(os.environ["RELAY_FAKE_OPEN_MARKER"], "w") as handle:
            handle.write(str(request_line.endswith("\\n") and is_open))
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
        if os.environ.get("RELAY_FAKE_STARTED_MARKER"):
            with open(os.environ["RELAY_FAKE_STARTED_MARKER"], "w") as handle:
                handle.write("started")
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
             "--no-session --no-pty --no-title")
    if scenario == "no_title":
        flags = flags.replace(" --no-title", "")
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
    if scenario in ("happy", "outside_root", "slow_result"):
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
    model = {"provider": "anthropic", "id": "claude-opus-5-5"}
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
    if scenario == "remote_failure":
        # The remote side died, so the bridge could not read session state.
        result.update(model=None, session_id=None, final_response=None,
                      failure="prompt failed: remote turn failed: overloaded")
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
    monkeypatch.setattr(relay_module, "_require_bun", lambda: "fake-bun")
    monkeypatch.setattr(relay_module, "_ensure_runtime", lambda runtime_dir: script)
    monkeypatch.setattr(
        relay_module,
        "_bridge_command",
        lambda bun, bridge: [sys.executable, str(bridge)],
    )
    return script


@pytest.fixture
def runtime_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    package_root = tmp_path / "package"
    source_root = Path(relay_module.__file__).resolve().parent
    for relative in relay_module.RELAY_SOURCE_FILES:
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / relative, target)
    state = tmp_path / "state"
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(state))
    count = tmp_path / "installs"
    monkeypatch.setenv("FAKE_INSTALL_COUNT", str(count))
    installer = tmp_path / "fake-bun"
    installer.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "count = pathlib.Path(os.environ['FAKE_INSTALL_COUNT'])\n"
        "count.write_text(count.read_text() + 'x' if count.exists() else 'x')\n"
        "if os.environ.get('FAKE_INSTALL_FAIL'): sys.exit(1)\n"
        "pathlib.Path('node_modules/@oh-my-pi/pi-coding-agent').mkdir(parents=True)\n"
    )
    installer.chmod(0o755)
    monkeypatch.setattr(relay_module, "_require_bun", lambda: str(installer))
    return package_root / "relay_runtime", state, count


def test_runtime_installs_outside_package_and_reuses_cache(
    runtime_setup: tuple[Path, Path, Path],
) -> None:
    runtime_dir, state, count = runtime_setup
    bridge = relay_module._ensure_runtime(runtime_dir)
    digest = relay_module.relay_source_sha256(runtime_dir.parent)
    assert bridge == state / "relay-runtime" / digest / "relay_runtime" / "relay_bridge.ts"
    assert bridge.is_file()
    assert not (runtime_dir / "node_modules").exists()
    assert count.read_text() == "x"
    assert relay_module._ensure_runtime(runtime_dir) == bridge
    assert count.read_text() == "x"


def test_runtime_refuses_changed_cached_source(
    runtime_setup: tuple[Path, Path, Path],
) -> None:
    runtime_dir, _, count = runtime_setup
    bridge = relay_module._ensure_runtime(runtime_dir)
    expected = relay_module.relay_source_sha256(runtime_dir.parent)
    bridge.write_text("tampered")
    actual = relay_module.relay_source_sha256(bridge.parents[1])
    with pytest.raises(relay_module.RelayError, match=f"package {expected}, cache {actual}"):
        relay_module._ensure_runtime(runtime_dir)
    assert count.read_text() == "x"


def test_failed_install_does_not_publish_cache(
    runtime_setup: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir, state, count = runtime_setup
    monkeypatch.setenv("FAKE_INSTALL_FAIL", "1")
    digest = relay_module.relay_source_sha256(runtime_dir.parent)
    with pytest.raises(relay_module.RelayError, match="bridge runtime install failed"):
        relay_module._ensure_runtime(runtime_dir)
    assert not (state / "relay-runtime" / digest).exists()
    assert list((state / "relay-runtime").iterdir()) == []
    assert not (runtime_dir / "node_modules").exists()
    assert count.read_text() == "x"


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
    assert record["model"] == {"provider": "anthropic", "id": "claude-opus-5-5"}
    assert len(record["relay_source_sha256"]) == 64
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
    assert request["model"] == "anthropic/claude-opus-5-5"
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


def _wait_marker(
    marker: Path, process: subprocess.Popen[bytes], *, allow_exit: bool = False
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if not allow_exit and process.poll() is not None:
            pytest.fail(f"relay child exited before {marker.name}: {process.returncode}")
        time.sleep(0.02)
    pytest.fail(f"timed out waiting for {marker.name}")


def _relay_child(relay_project: Path, fake_bridge: Path, scenario: str) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    source_root = str(Path(relay_module.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [source_root, env.get("PYTHONPATH")]))
    env.update(
        RELAY_FAKE_SCENARIO=scenario,
        RELAY_FAKE_STARTED_MARKER=str(_marker(relay_project, "started")),
        RELAY_FAKE_TERMINATED_MARKER=str(_marker(relay_project, "terminated")),
        RELAY_FAKE_EOF_MARKER=str(_marker(relay_project, "eof")),
    )
    code = """
from pathlib import Path
import sys
from agent_offload import relay
relay._require_bun = lambda: 'fake-bun'
relay._ensure_runtime = lambda runtime_dir: Path(sys.argv[2])
relay._bridge_command = lambda bun, bridge: [sys.executable, str(bridge)]
relay.run_relay(project_root=Path(sys.argv[1]), prompt='review this',
                host='agent@studio', timeout_seconds=60)
"""
    return subprocess.Popen(
        [sys.executable, "-c", code, str(relay_project), str(fake_bridge)],
        cwd=relay_project.parent,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_request_keeps_stdin_open_through_frames(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _marker(relay_project, "open")
    monkeypatch.setenv("RELAY_FAKE_OPEN_MARKER", str(marker))
    _run_scenario(relay_project, monkeypatch, "stdin_open")
    assert marker.read_text() == "True"


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="requires SIGHUP")
def test_sighup_unwinds_and_terminates_bridge(relay_project: Path, fake_bridge: Path) -> None:
    child = _relay_child(relay_project, fake_bridge, "sleep")
    try:
        _wait_marker(_marker(relay_project, "started"), child)
        child.send_signal(signal.SIGHUP)
        assert child.wait(timeout=10) != 0
        assert _marker(relay_project, "terminated").read_text() == "terminated"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_sigkill_closes_bridge_lifeline(relay_project: Path, fake_bridge: Path) -> None:
    child = _relay_child(relay_project, fake_bridge, "lifeline")
    try:
        _wait_marker(_marker(relay_project, "started"), child)
        child.kill()
        child.wait(timeout=10)
        _wait_marker(_marker(relay_project, "eof"), child, allow_exit=True)
        assert _marker(relay_project, "eof").read_text() == "eof"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_remote_failure_without_session_state_is_recorded_as_a_failure(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_path, record = _run_scenario(relay_project, monkeypatch, "remote_failure")
    assert record["failure"] == "prompt failed: remote turn failed: overloaded"
    assert record["model"] is None
    assert json.loads(result_path.read_text())["failure"] == record["failure"]


def test_success_without_model_attestation_is_still_refused() -> None:
    from agent_offload.relay_protocol import validate_relay_result

    record = {
        "schema_version": RESULT_SCHEMA,
        "harness": "omp",
        "host_target": "h",
        "host_identity": "h",
        "project_root": "/p",
        "prompt_sha256": "a" * 64,
        "relay_source_sha256": "b" * 64,
        "started_at": 1,
        "ended_at": 2,
        "durability": "none-attached-live-local-tools",
        "snapshot_equivalent": False,
        "model": None,
        "failure": None,
        "cancelled": False,
        "final_response": "done",
        "registered_tools": ["review_read", "review_glob", "review_grep"],
        "remote_events": [],
    }
    with pytest.raises(RelayProtocolError, match="result.model"):
        validate_relay_result(record, project_root="/p")
    record["model"] = {"provider": "anthropic", "id": "claude-opus-4-5"}
    record["failure"] = "prompt failed"
    with pytest.raises(RelayProtocolError, match="drifted"):
        validate_relay_result(record, project_root="/p")


def test_result_parsed_after_the_bridge_exits_is_not_lost(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reader thread is still parsing the result frame when the conductor
    # notices the bridge has exited; a large result stream parses slowly.
    validate = relay_module.validate_bridge_frame_kind

    def slow_result(frame):
        if frame.get("frame") == "result":
            time.sleep(6.5)
        return validate(frame)

    monkeypatch.setattr(relay_module, "validate_bridge_frame_kind", slow_result)
    _, record = _run_scenario(relay_project, monkeypatch, "slow_result")
    assert record["final_response"] == "done"


def test_refuses_remote_argv_without_title_suppression(
    relay_project: Path, fake_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RelayProtocolError, match="--no-title"):
        _run_scenario(relay_project, monkeypatch, "no_title")


def test_conductor_attests_exactly_the_flags_the_bridge_sets() -> None:
    import re

    source = (
        Path(relay_module.__file__).resolve().parent / "relay_runtime" / "remote_argv.ts"
    ).read_text()
    block = re.search(r"REMOTE_DISABLE_FLAGS = \[(.*?)\] as const", source, re.DOTALL)
    assert block is not None
    assert tuple(re.findall(r'"(--[a-z-]+)"', block.group(1))) == relay_module.REMOTE_DISABLE_FLAGS
