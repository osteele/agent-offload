"""Versioned schemas and validators for the attached local-tool relay.

The relay moves the agent runtime to a remote SSH host while every tool call
executes locally against the live project root. Unlike the durable Weft lane
there is no source snapshot, no worker, and no resume: the evidence is the
recorded RPC event stream plus the per-call provenance of every local read,
and the result says so explicitly.

Two schemas live here:

- ``agent-offload.relay-bridge/v1`` — the NDJSON envelope the Bun bridge
  emits on stdout. The conductor refuses an incomplete or malformed stream
  rather than treating it as a review-adjacent verdict.
- ``agent-offload.relay-result/v1`` — the conductor's durable output record.

The module is deliberately standalone: the relay is a narrow execution
boundary that will later consume a common remote-execution component, so it
imports nothing from the durable lane's worker or protocol modules.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

BRIDGE_SCHEMA = "agent-offload.relay-bridge/v1"
RESULT_SCHEMA = "agent-offload.relay-result/v1"

#: Host tools the bridge registers over RPC. The remote agent must never see
#: anything beyond this set: no built-ins, no bash, no edit paths.
RELAY_TOOLS = ("review_read", "review_glob", "review_grep")

#: Tool calls larger than this are refused by the bridge before execution.
MAX_TOOL_ARGS_BYTES = 64 * 1024
#: A single local read may return at most this many bytes.
MAX_READ_BYTES = 512 * 1024
#: Glob and grep may return at most this many files/matches per call.
MAX_MATCH_FILES = 500
MAX_GREP_MATCHES = 500
#: Glob and grep may traverse at most this many files per call, matching or
#: not — a nonmatching tree must terminate the call, not fund an endless walk.
MAX_TRAVERSED_FILES = 2000
#: Tool results are capped at this size after bounds are applied.
MAX_TOOL_RESULT_BYTES = 256 * 1024

#: The grounded OMP lane's sole sanctioned inference selector, split for
#: result-attestation binding.
RELAY_PROVIDER = "anthropic"
RELAY_MODEL_ID = "claude-opus-5-5"
RELAY_SELECTOR = f"{RELAY_PROVIDER}/{RELAY_MODEL_ID}"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RelayProtocolError(RuntimeError):
    """The bridge stream or the relay result violated its schema."""


def _require(mapping: Mapping[str, Any], key: str, types: tuple[type, ...], where: str) -> Any:
    value = mapping.get(key)
    if not isinstance(value, types):
        raise RelayProtocolError(f"{where}.{key} missing or wrong type")
    return value


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_bridge_frame(frame: Mapping[str, Any]) -> str:
    """Validate one bridge envelope frame; returns its ``frame`` kind.

    Ready frames are validated deeply (identity fields and the exact
    registered tool set) because the conductor's downstream checks are
    indexed against them; a malformed ready must fail closed here.
    """
    kind = _require(frame, "frame", (str,), "bridge_frame")
    if kind not in {"ready", "event", "result", "error"}:
        raise RelayProtocolError(f"bridge_frame.frame unknown kind {kind!r}")
    if frame.get("schema_version") != BRIDGE_SCHEMA:
        raise RelayProtocolError(f"bridge_frame.schema_version must be {BRIDGE_SCHEMA!r}")
    if kind == "ready":
        for key in ("sdk_version", "omp_version", "host_identity", "remote_argv"):
            _require(frame, key, (str,), "bridge_frame")
        registered = frame.get("registered_tools")
        if sorted(str(tool) for tool in (registered or [])) != sorted(RELAY_TOOLS):
            raise RelayProtocolError(
                "ready registered_tools must be exactly "
                f"{sorted(RELAY_TOOLS)}; observed {sorted(str(t) for t in (registered or []))}"
            )
    elif kind == "event":
        _require(frame, "event", (dict,), "bridge_frame")
    elif kind == "result":
        _require(frame, "result", (dict,), "bridge_frame")
    elif kind == "error":
        _require(frame, "error", (dict,), "bridge_frame")
    return kind


def validate_complete_tool_lifecycle(events: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every started tool call must have exactly one completed result.

    Returns the paired calls in start order. A stream where a call never
    resolves, resolves twice, or resolves against the wrong id is an
    observation failure, not a review output, so the conductor refuses the
    whole result.
    """
    started: dict[str, dict[str, Any]] = {}
    paired: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        kind = event.get("type")
        if kind == "tool_execution_start":
            call_id = event.get("toolCallId")
            tool = event.get("toolName")
            if not isinstance(call_id, str) or not isinstance(tool, str):
                raise RelayProtocolError(f"event {index}: malformed tool_execution_start")
            if tool not in RELAY_TOOLS:
                raise RelayProtocolError(
                    f"event {index}: unsanctioned relay tool {tool!r}; "
                    f"sanctioned tools are {sorted(RELAY_TOOLS)}"
                )
            if call_id in started:
                raise RelayProtocolError(f"event {index}: tool call {call_id!r} started twice")
            started[call_id] = {"tool_call_id": call_id, "tool": tool}
        elif kind == "tool_execution_end":
            call_id = event.get("toolCallId")
            if not isinstance(call_id, str) or call_id not in started:
                raise RelayProtocolError(f"event {index}: tool result without started call")
            record = started.pop(call_id)
            result = event.get("result")
            if not isinstance(result, Mapping) or not isinstance(result.get("content"), list):
                raise RelayProtocolError(f"event {index}: tool result lacks content")
            record["is_error"] = bool(event.get("isError", result.get("isError", False)))
            record["result"] = result
            paired.append(record)
    if started:
        unresolved = ", ".join(sorted(started))
        raise RelayProtocolError(f"tool calls without results: {unresolved}")
    return paired


def _validate_served_paths(call: Mapping[str, Any], root: str) -> None:
    for path in call.get("served_paths", []):
        if not isinstance(path, Mapping):
            raise RelayProtocolError("tool call served_paths entries must be objects")
        resolved = path.get("path")
        if not isinstance(resolved, str) or not resolved.startswith(root + "/"):
            raise RelayProtocolError(
                f"tool call {call.get('tool_call_id')!r} served {resolved!r} outside root"
            )
        if not _SHA256.match(str(path.get("sha256", ""))):
            raise RelayProtocolError(
                f"tool call {call.get('tool_call_id')!r} served path lacks content digest"
            )


BRIDGE_RESULT_REQUIRED = (
    "sdk_version",
    "omp_version",
    "host_target",
    "host_identity",
    "remote_argv",
    "remote_scratch",
    "registered_tools",
    "session_id",
    "session_file",
    "model",
    "started_at",
    "ended_at",
    "failure",
    "cancelled",
    "events",
    "final_response",
    "stats",
    "prompt_sha256",
    "bounds",
)


def validate_bridge_result_payload(payload: Mapping[str, Any]) -> None:
    """Refuse a result frame whose required keys are absent (None allowed)."""
    missing = [key for key in BRIDGE_RESULT_REQUIRED if key not in payload]
    if missing:
        observed = sorted(str(key) for key in payload)
        raise RelayProtocolError(
            "bridge result payload missing keys: "
            + ", ".join(repr(key) for key in missing)
            + f"; observed keys ({len(observed)}): {observed}"
        )


def validate_relay_result(result: Mapping[str, Any], *, project_root: str) -> dict[str, Any]:
    """Validate the conductor-side result record against the v1 schema.

    The guard rails that matter: the record never claims durability, every
    tool call is paired with complete evidence, and every served path is
    inside the project root with a content digest.
    """
    if result.get("schema_version") != RESULT_SCHEMA:
        raise RelayProtocolError(f"result.schema_version must be {RESULT_SCHEMA!r}")
    _require(result, "harness", (str,), "result")
    _require(result, "host_target", (str,), "result")
    _require(result, "host_identity", (str,), "result")
    _require(result, "project_root", (str,), "result")
    if result["project_root"] != project_root:
        raise RelayProtocolError("result.project_root differs from the requested root")
    _require(result, "prompt_sha256", (str,), "result")
    if not _SHA256.match(str(result["prompt_sha256"])):
        raise RelayProtocolError("result.prompt_sha256 malformed")
    _require(result, "relay_source_sha256", (str,), "result")
    if not _SHA256.match(str(result["relay_source_sha256"])):
        raise RelayProtocolError("result.relay_source_sha256 malformed")
    _require(result, "started_at", (float, int), "result")
    _require(result, "ended_at", (float, int), "result")
    durability = _require(result, "durability", (str,), "result")
    if durability != "none-attached-live-local-tools":
        raise RelayProtocolError(
            f"result.durability must state the attached lane, got {durability!r}"
        )
    if result.get("snapshot_equivalent") is not False:
        raise RelayProtocolError("result.snapshot_equivalent must be false")
    model = _require(result, "model", (dict,), "result")
    _require(model, "provider", (str,), "result.model")
    _require(model, "id", (str,), "result.model")
    # Selector attestation: the evidence binds to the pinned grounded route,
    # so SDK/server-side drift cannot hide inside an otherwise valid result.
    if model["provider"] != RELAY_PROVIDER or model["id"] != RELAY_MODEL_ID:
        raise RelayProtocolError(
            "result model drifted from the pinned grounded selector "
            f"{RELAY_PROVIDER}/{RELAY_MODEL_ID}: "
            f"{model['provider']}/{model['id']}"
        )
    failure = result.get("failure")
    if failure is not None and not isinstance(failure, str):
        raise RelayProtocolError("result.failure must be a string or null")
    cancelled = result.get("cancelled")
    if not isinstance(cancelled, bool):
        raise RelayProtocolError("result.cancelled must be a boolean")
    final_response = result.get("final_response")
    if final_response is not None and not isinstance(final_response, str):
        raise RelayProtocolError("result.final_response must be a string or null")
    if (
        failure is None
        and not cancelled
        and not (isinstance(final_response, str) and final_response.strip())
    ):
        raise RelayProtocolError("a success result requires a non-empty assistant response")
    registered = result.get("registered_tools")
    if sorted(str(tool) for tool in (registered or [])) != sorted(RELAY_TOOLS):
        raise RelayProtocolError(
            "result registered_tools must be exactly "
            f"{sorted(RELAY_TOOLS)}; observed {sorted(str(t) for t in (registered or []))}"
        )
    events = _require(result, "remote_events", (list,), "result")
    calls = validate_complete_tool_lifecycle(events)
    for call in calls:
        result_details = call.get("result", {}).get("details")
        call["served_paths"] = (
            result_details.get("served_paths", []) if isinstance(result_details, Mapping) else []
        )
        _validate_served_paths(call, project_root)
    checked: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "tool_calls": calls,
        "tool_call_count": len(calls),
    }
    response = result.get("final_response")
    checked["final_response"] = response if isinstance(response, str) else None
    return checked
