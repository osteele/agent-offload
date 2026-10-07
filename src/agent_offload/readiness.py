"""Execution-owned observations and Offload's task admission policy."""

from __future__ import annotations

from agent_execution.execution_status import probe_remote_status

from .task_protocol import TaskError

POLICY = "offload-task/v1"
REQUIRED_FACTS = ("capability", "authentication", "transport")
REPORTED_FACTS = ("capability", "authentication", "quota", "generation", "transport")


def probe_status(
    *,
    host: str,
    account: str = "agent",
    selectors: list[str],
    bin_dir: str | None = None,
    execution_sha256: str | None = None,
) -> dict:
    """Observe through the exact Offload bundle, not Review's global worker."""
    try:
        return probe_remote_status(
            host=host,
            account=account,
            harness="omp",
            surface="offload-task",
            selectors=selectors,
            transport="weft",
            bin_dir=bin_dir,
            worker_executable="agent-offload-worker",
            expected_execution_sha256=execution_sha256,
        )
    except (OSError, ValueError) as error:
        raise TaskError(f"Execution status probe on {host} failed: {error}") from error


def admission(status: dict, selectors: list[str]) -> dict:
    """Apply workload policy to an Execution-validated observation envelope."""
    rows = {row["subject"]["selector"]: row for row in status["rows"]}
    reasons = [] if selectors else ["No task writers selected"]
    for selector in selectors:
        row = rows.get(selector)
        if row is None:
            reasons.append(f"{selector}: no observation for the requested writer")
            continue
        facts = row["facts"]
        for name in REQUIRED_FACTS:
            fact = facts[name]
            if fact["state"] != "available" or fact["stale"]:
                freshness = "stale" if fact["stale"] else "current"
                reasons.append(
                    f"{selector}: {name} is {fact['state']} ({freshness}): {fact['detail']}"
                )
        quota = facts["quota"]
        if quota["state"] == "unavailable" and not quota["stale"]:
            reasons.append(f"{selector}: current quota refusal: {quota['detail']}")
    return {"policy": POLICY, "allowed": not reasons, "reasons": reasons}


def require_writer(record: dict) -> None:
    """Refuse a new submission unless its pinned writer satisfies task policy."""
    selector = record["request"]["selector"]
    status = probe_status(
        host=record["host"],
        account=record.get("account", "agent"),
        selectors=[selector],
        bin_dir=record["bin_dir"],
        execution_sha256=record["request"]["execution_sha256"],
    )
    decision = admission(status, [selector])
    if not decision["allowed"]:
        raise TaskError("; ".join(decision["reasons"]))
