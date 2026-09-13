"""Attached relay conductor: remote agent runtime, local project tools.

``agent-offload relay`` launches an OMP agent on a remote SSH host through
the maintained RPC client while the three confined host tools (read, glob,
grep) execute here, against the live project root. The lane is deliberately
attached: no snapshot, no worker, no resume, no detach. The evidence is the
full remote event stream plus per-call provenance for every local read, and
the result record states those limits rather than borrowing the durable
lane's guarantees.

The conductor enforces the trust boundary from the Python side: it validates
every bridge frame against a versioned schema, refuses an incomplete tool
lifecycle, refuses served paths outside the project root, bounds the whole
request in time, and terminates the remote process tree on exit or
cancellation. The Bun bridge under ``relay_runtime/`` owns the RPC protocol
itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .relay_protocol import (
    BRIDGE_SCHEMA,
    MAX_GREP_MATCHES,
    MAX_MATCH_FILES,
    MAX_READ_BYTES,
    MAX_TOOL_ARGS_BYTES,
    MAX_TOOL_RESULT_BYTES,
    MAX_TRAVERSED_FILES,
    RELAY_PROVIDER,
    RELAY_SELECTOR,
    RESULT_SCHEMA,
    RelayProtocolError,
    sha256_text,
    validate_bridge_result_payload,
    validate_relay_result,
)

BRIDGE_FILENAME = "relay_bridge.ts"
RUNTIME_PACKAGE = "@oh-my-pi/pi-coding-agent"
LOCAL_CONTEXT_FILES = ("CLAUDE.md", "AGENTS.md")
LOCAL_CONTEXT_MAX_BYTES = 32 * 1024
#: Hard wall for the whole relay request, whatever the bridge is doing.
DEFAULT_TIMEOUT_SECONDS = 600.0
#: Extra wall-clock allowed beyond the request timeout for preflight and
#: teardown before the conductor kills the bridge process group.
RELAY_DEADLINE_GRACE_SECONDS = 30.0

TERMINATION_GRACE_SECONDS = 10.0
RELAY_SOURCE_FILES = (
    "relay.py",
    "relay_protocol.py",
    "relay_runtime/bun.lock",
    "relay_runtime/confinement.ts",
    "relay_runtime/event_failure.ts",
    "relay_runtime/package.json",
    "relay_runtime/relay_bridge.ts",
)


def relay_source_sha256() -> str:
    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for relative in RELAY_SOURCE_FILES:
        encoded_path = relative.encode()
        payload = (package_root / relative).read_bytes()
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


#: Flags the remote argv must carry for the conductor to accept a result.
#: The bridge enforces the same set when building the argv; this check is
#: the conductor's independent attestation that built-in tools stayed off.
REMOTE_DISABLE_FLAGS = (
    "--no-tools",
    "--no-lsp",
    "--no-extensions",
    "--no-skills",
    "--no-rules",
    "--no-session",
    "--no-pty",
)

TRUST_EPILOG = """trust boundary:
  attached     this command stays in the foreground; the remote agent dies
               with it. There is no job id, no resume, and no --detach.
  live-local   the remote agent's three tools (review_read, review_glob,
               review_grep) execute HERE against the live project root, so
               the model can observe files changing under it. Nothing is
               snapshot-backed and no durability is promised or implied.
  confined     paths are canonicalized and must resolve inside the project
               root; URL schemes, devices, symlink escapes, binary and
               oversized inputs are refused. Output, matches, files, and
               wall time are bounded.
  pinned       the inference selector is fixed to
               anthropic/claude-opus-4-6; no --model/--provider override
               exists on this lane.
  fail-closed  if the remote side cannot confirm that built-in tools are
               disabled and exactly the three host tools are registered,
               the relay refuses instead of degrading.
"""


class RelayError(RuntimeError):
    """The relay could not run, or failed closed."""


@dataclass(frozen=True)
class RelayBounds:
    max_read_bytes: int = MAX_READ_BYTES
    max_match_files: int = MAX_MATCH_FILES
    max_grep_matches: int = MAX_GREP_MATCHES
    max_tool_result_bytes: int = MAX_TOOL_RESULT_BYTES
    max_traversed_files: int = MAX_TRAVERSED_FILES
    max_tool_args_bytes: int = MAX_TOOL_ARGS_BYTES

    def as_dict(self) -> dict[str, int]:
        return {
            "maxReadBytes": self.max_read_bytes,
            "maxMatchFiles": self.max_match_files,
            "maxGrepMatches": self.max_grep_matches,
            "maxToolResultBytes": self.max_tool_result_bytes,
            "maxTraversedFiles": self.max_traversed_files,
            "maxToolArgsBytes": self.max_tool_args_bytes,
        }


def _relay_prompt(prompt: str, system_prompt: str | None) -> str:
    """The exact prompt the bridge sends to the agent.

    Cross-language contract: relay_bridge.ts combines prompt and system
    prompt with this same formulation before digesting, and the conductor
    binds the bridge's recorded digest to its own computation of it.
    """
    if not system_prompt:
        return prompt
    return f"<project-context>\n{system_prompt}\n</project-context>\n\n{prompt}"


def _local_context(project_root: Path) -> list[dict[str, str]]:
    """Project instructions resolved locally and shipped to the agent
    explicitly, so the remote host never discovers context of its own."""
    blocks: list[dict[str, str]] = []
    for name in LOCAL_CONTEXT_FILES:
        path = project_root / name
        if not path.is_file():
            continue
        content = path.read_bytes()
        if len(content) > LOCAL_CONTEXT_MAX_BYTES:
            content = content[:LOCAL_CONTEXT_MAX_BYTES]
        blocks.append(
            {
                "name": name,
                "sha256": sha256_text(content.decode("utf-8", errors="replace")),
                "content": content.decode("utf-8", errors="replace"),
            }
        )
    return blocks


def _require_bun() -> str:
    bun = shutil_which("bun")
    if bun is None:
        raise RelayError("relay requires bun on PATH to run its RPC bridge")
    return bun


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def _ensure_runtime(runtime_dir: Path) -> Path:
    bridge = runtime_dir / BRIDGE_FILENAME
    if not bridge.is_file():
        raise RelayError(f"relay bridge missing: {bridge}")
    package_dir = (
        runtime_dir / "node_modules" / RUNTIME_PACKAGE.split("/")[0] / RUNTIME_PACKAGE.split("/")[1]
    )
    if not package_dir.is_dir():
        bun = _require_bun()
        print(
            "relay: installing pinned bridge runtime (bun install --frozen-lockfile)",
            file=sys.stderr,
        )
        completed = subprocess.run(
            [bun, "install", "--frozen-lockfile"],
            cwd=runtime_dir,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if completed.returncode != 0:
            raise RelayError(
                "bridge runtime install failed: " + (completed.stderr or completed.stdout)[-400:]
            )
    return bridge


def _terminate_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            process.terminate()
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait(timeout=10)


def _read_frames(
    process: subprocess.Popen[str],
    frames: queue.Queue[tuple[str, Any] | None],
) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            kind = validate_bridge_frame_kind(parsed)
        except (json.JSONDecodeError, RelayProtocolError) as error:
            frames.put(("protocol_error", str(error)))
            return
        frames.put((kind, parsed))
    frames.put((None, None))


def _bridge_command(bun: str, bridge: Path) -> list[str]:
    return [bun, "run", str(bridge)]


def validate_bridge_frame_kind(frame: dict[str, Any]) -> str:
    from .relay_protocol import validate_bridge_frame

    return validate_bridge_frame(frame)


def run_relay(
    *,
    project_root: Path,
    prompt: str,
    host: str,
    timeout_seconds: float,
) -> tuple[Path, dict[str, Any]]:
    """Run one attached relay request; returns (result_path, record)."""
    if not project_root.is_dir():
        raise RelayError(f"project is not a directory: {project_root}")
    bun = _require_bun()
    runtime_dir = Path(__file__).resolve().parent / "relay_runtime"
    bridge = _ensure_runtime(runtime_dir)
    relay_source_before = relay_source_sha256()

    context_blocks = _local_context(project_root)
    system_prompt = "\n\n".join(
        f'<local-project-file name="{block["name"]}">\n{block["content"]}\n</local-project-file>'
        for block in context_blocks
    )
    request = {
        "schema_version": "agent-offload.relay-request/v1",
        "project_root": str(project_root),
        "prompt": prompt,
        "system_prompt": system_prompt or None,
        "host_target": host,
        "provider": RELAY_PROVIDER,
        "model": RELAY_SELECTOR,
        "timeout_seconds": timeout_seconds,
        "bounds": RelayBounds().as_dict(),
    }
    if request["provider"] != RELAY_PROVIDER or request["model"] != RELAY_SELECTOR:
        raise RelayProtocolError(
            f"relay selector drifted from the pinned grounded route {RELAY_SELECTOR}"
        )

    process = subprocess.Popen(
        _bridge_command(bun, bridge),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        start_new_session=True,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(request))
    process.stdin.close()

    frames: queue.Queue[tuple[str, Any] | None] = queue.Queue()
    reader = threading.Thread(target=_read_frames, args=(process, frames), daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout_seconds + RELAY_DEADLINE_GRACE_SECONDS
    event_count = 0
    bridge_result: dict[str, Any] | None = None
    bridge_error: str | None = None
    timed_out = False
    ready_seen = False
    streamed_events: list[dict[str, Any]] = []

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                kind, payload = frames.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                if process.poll() is not None and frames.empty():
                    break
                continue
            if kind is None:
                break
            if kind == "protocol_error":
                raise RelayProtocolError(f"bridge stream violated its schema: {payload}")
            if kind == "ready":
                if ready_seen:
                    raise RelayProtocolError("duplicate ready frame in bridge stream")
                ready_seen = True
                print(
                    f"relay: agent ready on {host} "
                    f"(omp {payload['omp_version']}); tools {payload['registered_tools']}",
                    file=sys.stderr,
                )
            elif kind == "event":
                if not ready_seen:
                    raise RelayProtocolError("event frame arrived before the ready frame")
                event_count += 1
                streamed_events.append(payload["event"])
                name = payload["event"].get("type")
                if name in {"tool_execution_start", "message_update"}:
                    print(f"relay: event {event_count}: {name}", file=sys.stderr)
            elif kind == "result":
                if not ready_seen:
                    raise RelayProtocolError("result frame arrived before the ready frame")
                bridge_result = payload["result"]
                break
            elif kind == "error":
                bridge_error = str(payload["error"].get("message", "bridge error"))
                break
    finally:
        if process.poll() is None:
            _terminate_group(process)
        reader.join(timeout=5)
    if bridge_error is not None:
        raise RelayError(f"relay bridge failed closed: {bridge_error}")
    if timed_out and bridge_result is None:
        raise RelayError(f"relay exceeded {timeout_seconds:g}s; remote process tree terminated")
    if bridge_result is None:
        raise RelayError(
            f"bridge exited without a result frame (exit={process.returncode}); "
            "refusing to treat a partial stream as an outcome"
        )
    validate_bridge_result_payload(bridge_result)
    # The streamed event frames are the primary evidence: the result frame's
    # event list must match them exactly, so the bridge cannot omit or
    # substitute lifecycle evidence between streaming and settlement.
    if bridge_result["events"] != streamed_events:
        raise RelayProtocolError("result events differ from the streamed event frames")
    if bridge_result["host_target"] != host:
        raise RelayProtocolError(
            f"bridge host_target drift: {bridge_result['host_target']!r} != {host!r}"
        )
    if bridge_result["bounds"] != request["bounds"]:
        raise RelayProtocolError(
            "bridge bounds drift from the conductor bounds: "
            f"{bridge_result['bounds']!r} != {request['bounds']!r}"
        )
    expected_prompt_sha256 = sha256_text(_relay_prompt(prompt, system_prompt))
    if bridge_result["prompt_sha256"] != expected_prompt_sha256:
        raise RelayProtocolError("bridge prompt digest does not match the conductor prompt")

    try:
        argv_tokens = shlex.split(str(bridge_result["remote_argv"]))
    except ValueError as exc:
        raise RelayProtocolError(f"remote argv is not valid shell syntax: {exc}") from exc
    relay_source_after = relay_source_sha256()
    if relay_source_after != relay_source_before:
        raise RelayProtocolError("relay implementation changed during the attached request")
    missing = [flag for flag in REMOTE_DISABLE_FLAGS if flag not in argv_tokens]
    if missing:
        raise RelayProtocolError(
            "remote argv lacks required tool-disable flags: " + ", ".join(missing)
        )

    events = bridge_result["events"]
    record: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "harness": "omp",
        "bridge_schema": BRIDGE_SCHEMA,
        "sdk_version": bridge_result["sdk_version"],
        "omp_version": bridge_result["omp_version"],
        "host_target": bridge_result["host_target"],
        "host_identity": bridge_result["host_identity"],
        "remote_argv": bridge_result["remote_argv"],
        "remote_scratch": bridge_result["remote_scratch"],
        "registered_tools": bridge_result["registered_tools"],
        "session_id": bridge_result["session_id"],
        "session_file": bridge_result["session_file"],
        "model": bridge_result["model"],
        "project_root": str(project_root),
        "context_files": [
            {"name": block["name"], "sha256": block["sha256"]} for block in context_blocks
        ],
        "relay_source_sha256": relay_source_before,
        "prompt_sha256": bridge_result["prompt_sha256"],
        "system_prompt_sha256": sha256_text(system_prompt) if system_prompt else None,
        "started_at": bridge_result["started_at"],
        "ended_at": bridge_result["ended_at"],
        "durability": "none-attached-live-local-tools",
        "snapshot_equivalent": False,
        "cancelled": bridge_result["cancelled"],
        "failure": bridge_result["failure"],
        "bounds": bridge_result["bounds"],
        "remote_events": events,
        "final_response": bridge_result["final_response"],
        "stats": bridge_result["stats"],
    }
    checks = validate_relay_result(record, project_root=str(project_root))
    record["tool_calls"] = checks["tool_calls"]
    record["tool_call_count"] = checks["tool_call_count"]

    outputs = Path.cwd() / "outputs"
    outputs.mkdir(exist_ok=True)
    result_path = outputs / f"relay-result-{uuid.uuid4().hex[:12]}.json"
    result_path.write_text(json.dumps(record, sort_keys=True, indent=2))
    return result_path, record


def cmd_relay(args: argparse.Namespace) -> int:
    import shutil

    if args.detach:
        print(
            "agent-offload: the relay lane is attached by design; --detach is "
            "not available and durable submission is retired.",
            file=sys.stderr,
        )
        return 2
    if bool(args.prompt) == bool(args.prompt_file):
        print(
            "agent-offload: relay needs exactly one of --prompt or --prompt-file", file=sys.stderr
        )
        return 2
    if args.timeout <= 0:
        print("agent-offload: --timeout must be positive", file=sys.stderr)
        return 2
    del shutil

    project_root = args.project.expanduser().resolve()
    prompt = (
        args.prompt
        if args.prompt
        else args.prompt_file.expanduser().resolve().read_text(encoding="utf-8")
    )
    try:
        result_path, record = run_relay(
            project_root=project_root,
            prompt=prompt,
            host=args.host,
            timeout_seconds=args.timeout,
        )
    except RelayProtocolError as exc:
        print(f"agent-offload: relay evidence refused: {exc}", file=sys.stderr)
        return 3
    except RelayError as exc:
        print(f"agent-offload: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"result_path": str(result_path), "result": record}, sort_keys=True))
        return 4 if record["failure"] else 0
    print("=== ATTACHED RELAY RESULT (live-local tools; not durable; no snapshot) ===")
    if record["failure"]:
        print(f"failure: {record['failure']}")
    if record["model"]:
        print(f"model: {record['model']['provider']}/{record['model']['id']}")
    print(f"host: {record['host_target']} ({record['omp_version']})")
    print(f"tool calls: {record['tool_call_count']} (served paths digested in {result_path})")
    print(f"result: {result_path}")
    print("--- final response ---")
    print(record["final_response"] or "(no assistant text recorded)")
    return 4 if record["failure"] else 0


def build_relay_parser(subparsers: argparse._SubParsersAction) -> None:
    relay = subparsers.add_parser(
        "relay",
        help="run an attached remote-agent relay with locally confined project tools",
        description="Remote agent runtime, local live-tree tool execution. See the trust boundary in --help output.",
        epilog=TRUST_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    relay.add_argument("project", nargs="?", type=Path, default=Path.cwd())
    relay.add_argument("--prompt", "-p", help="task prompt for the remote agent")
    relay.add_argument("--prompt-file", type=Path, help="read the prompt from a file")
    relay.add_argument(
        "--host",
        default="agent@studio",
        help="headless SSH target for the agent runtime (the agent@ worker account, not a personal login)",
    )
    relay.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    relay.add_argument("--json", action="store_true")
    relay.add_argument("--detach", action="store_true", help="rejected: the relay lane is attached")
    relay.set_defaults(handler=cmd_relay)
