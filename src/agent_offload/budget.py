"""Renewable time budgets for durable coding tasks.

A task runs either under one fixed `--timeout` -- the writer's hard limit,
recorded as `request.timeout` with no `budget` object, exactly as requests
stored by older releases -- or under Execution's renewable budget policy.
The policy is persisted in the immutable request as four numeric fields,
re-validated at worker entry, and converted back into Execution's typed
`RenewableBudget` before the writer is launched. Only the initial amount
and the finite cap are caller-chosen; the extension and the progress window
are fixed by the approved policy, and a fixed timeout never renews.
"""

from __future__ import annotations

import math

from agent_execution.budget import RenewableBudget

from .task_protocol import TaskError

# The approved renewable policy for a fresh `task submit` with no time flags.
APPROVED_BUDGET: dict[str, float] = {
    "initial_seconds": 3600,
    "extension_seconds": 1800,
    "progress_window_seconds": 900,
    "max_seconds": 14400,
}
# Fixed by the approved policy; the CLI exposes no knobs for them.
FIXED_EXTENSION_SECONDS = APPROVED_BUDGET["extension_seconds"]
FIXED_WINDOW_SECONDS = APPROVED_BUDGET["progress_window_seconds"]
# The cap is a finite absolute limit: renewal never reaches past the approved
# four-hour ceiling, and a fixed `--timeout` never reaches it at all.
CAP_CEILING_SECONDS = APPROVED_BUDGET["max_seconds"]
# The same bound as a fixed `--timeout`: the renewable initial amount.
INITIAL_CEILING_SECONDS = 7200
BUDGET_FIELDS = tuple(sorted(APPROVED_BUDGET))


def budget_choice(initial: float, cap: float | None = None) -> dict[str, float]:
    """The persisted policy object for an explicit `--budget-initial` choice.

    The cap defaults to the approved finite ceiling; the extension and the
    progress window are the approved fixed values.
    """
    return {
        "initial_seconds": initial,
        "extension_seconds": FIXED_EXTENSION_SECONDS,
        "progress_window_seconds": FIXED_WINDOW_SECONDS,
        "max_seconds": CAP_CEILING_SECONDS if cap is None else cap,
    }


def _seconds(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskError(f"budget {name} must be a number of seconds")
    try:
        seconds = float(value)
    except OverflowError:
        # An integer too large for a float is not finite either, and the
        # worker must refuse it as an ordinary publishable failure.
        seconds = math.inf
    if not math.isfinite(seconds):
        raise TaskError(f"budget {name} must be a finite number of seconds")
    return seconds


def validate_budget(budget: object, *, timeout: object) -> dict[str, float]:
    """Validate a request's complete budget policy and normalize its numbers.

    Every rejection happens before persistence or generation: a missing or
    unexpected field, a boolean, NaN, infinity, a value outside the documented
    bounds, a cap that cannot accommodate the fixed extension, a policy that
    is not an object, and an initial amount inconsistent with the retained
    `timeout` (which is the renewable initial amount) all refuse. Execution's
    typed `RenewableBudget` remains the authority on the policy's own
    invariants and is constructed here, so a policy the executor would refuse
    never persists either. Returns the four normalized numeric fields.
    """
    if not isinstance(budget, dict):
        raise TaskError("task budget must be a policy object")
    if set(budget) != set(BUDGET_FIELDS):
        raise TaskError("task budget must have exactly the fields: " + ", ".join(BUDGET_FIELDS))
    fields = {name: _seconds(name, budget[name]) for name in BUDGET_FIELDS}
    if fields["extension_seconds"] != FIXED_EXTENSION_SECONDS:
        raise TaskError(
            f"budget extension_seconds is fixed at {FIXED_EXTENSION_SECONDS} seconds; "
            f"{fields['extension_seconds']!r} is not the approved policy"
        )
    if fields["progress_window_seconds"] != FIXED_WINDOW_SECONDS:
        raise TaskError(
            f"budget progress_window_seconds is fixed at {FIXED_WINDOW_SECONDS} seconds; "
            f"{fields['progress_window_seconds']!r} is not the approved policy"
        )
    if not 1 <= fields["initial_seconds"] <= INITIAL_CEILING_SECONDS:
        raise TaskError(
            f"budget initial_seconds {fields['initial_seconds']!r} is outside the "
            f"permitted range (1-{INITIAL_CEILING_SECONDS} seconds)"
        )
    timeout_seconds = _seconds("timeout", timeout)
    if fields["initial_seconds"] != timeout_seconds:
        raise TaskError(
            "budget initial_seconds must equal the retained timeout: for a budgeted "
            "request the timeout is the renewable initial amount, not a fixed limit"
        )
    if not 0 < fields["max_seconds"] <= CAP_CEILING_SECONDS:
        raise TaskError(
            f"budget max_seconds {fields['max_seconds']!r} must be a finite positive "
            f"number of at most {CAP_CEILING_SECONDS} seconds"
        )
    try:
        RenewableBudget(**fields)
    except ValueError as error:
        raise TaskError(f"task budget is not a valid renewable policy: {error}") from error
    return fields


def renewable_policy(budget: object, *, timeout: float) -> RenewableBudget:
    """The typed policy a budgeted request's worker launches its writer with."""
    return RenewableBudget(**validate_budget(budget, timeout=timeout))
