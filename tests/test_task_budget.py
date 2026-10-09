"""Consumer regressions for the two task time choices: the fixed `--timeout`
hard limit and the approved renewable budget policy."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

import pytest
from agent_execution.budget import BudgetClock, RenewableBudget
from agent_execution.identity import source_sha256
from test_task_outcomes import task_factory as parent_task_factory

from agent_offload import task_worker, tasks
from agent_offload.protocol import package_source_sha256
from agent_offload.task_protocol import SCHEMA, TaskError, digest, result_path

task_factory = parent_task_factory

APPROVED = {
    "initial_seconds": 3600,
    "extension_seconds": 1800,
    "progress_window_seconds": 900,
    "max_seconds": 14400,
}
SELECTOR = "openai-codex/gpt-6-astra"


# --- helpers -----------------------------------------------------------------


def budgeted_submission(tmp_path, monkeypatch, identity: str) -> tuple[Path, Path]:
    """A minimal project whose fake rg/weft accept `weft run` and log every argv."""
    from test_tasks import fake_tools

    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "hello.py").write_text("hello")
    calls = tmp_path / "weft-calls"
    receipt = json.dumps(
        {
            "api_version": "weft.run.receipt.v1",
            "job_id": "wj-budget",
            "placement_decision": "queued",
            "selected_host": "studio",
            "source_pin": "pin",
            "accepted_immediately": False,
            "deduplicated": False,
            "idempotency_key": identity,
        }
    )
    fake_tools(
        tmp_path,
        monkeypatch,
        files=["hello.py"],
        excludes=[],
        run=(f'echo "$*" >> {shlex.quote(str(calls))}\n/bin/echo {shlex.quote(receipt)}\n'),
    )
    return project, calls


def submit_args(project: Path, identity: str, **fields) -> argparse.Namespace:
    """A parser-shaped submit Namespace; the new budget fields may be omitted."""
    args = argparse.Namespace(
        action="submit",
        project=project,
        prompt="Fix hello",
        brief=None,
        host="studio",
        account=None,
        selector=SELECTOR,
        check="",
        id=identity,
        timeout=fields.pop("timeout", None),
    )
    for name in ("budget_initial", "budget_cap"):
        if name in fields:
            setattr(args, name, fields.pop(name))
    assert not fields
    return args


def continue_args(parent: str, *, timeout=None, budget_initial=None, budget_cap=None, id=None):
    """A parser-shaped continue Namespace; the new budget fields may be omitted."""
    args = argparse.Namespace(
        action="continue", parent=parent, timeout=timeout, prompt=None, brief=None, id=id
    )
    if budget_initial is not None:
        args.budget_initial = budget_initial
    if budget_cap is not None:
        args.budget_cap = budget_cap
    return args


def wall_time(calls: Path) -> str:
    """The --wall-time of the `weft run` in a shell-quoted call log."""
    argv = next(line.split() for line in calls.read_text().splitlines() if line.startswith("run "))
    return argv[argv.index("--wall-time") + 1]


def json_wall_time(calls: Path) -> str:
    """The --wall-time of the `weft run` in the task factory's JSON call log."""
    run = next(
        json.loads(line) for line in calls.read_text().splitlines() if json.loads(line)[0] == "run"
    )
    return run[run.index("--wall-time") + 1]


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": 1.0},
        {"decisions": {}},
        {"decisions": ["extended"]},
        {"termination_reason": ["completed"]},
        {"qualifying_progress_count": float("nan")},
        {"changed_file_count": True},
        {"duration_seconds": -1},
        {"decisions": [{"at_seconds": 2, "evidence_age_seconds": None, "decision": "extended"}]},
        {"decisions": [{"at_seconds": 0, "evidence_age_seconds": None, "decision": []}]},
    ],
)
def test_malformed_worker_budget_refused_by_parse_and_retrieval(task_factory, tmp_path, mutation):
    from agent_execution.worker import WorkerResult

    record, result = task_factory("malformed-budget")
    stats = {
        "schema_version": 1,
        "policy": dict(APPROVED),
        "duration_seconds": 1,
        "termination_reason": "completed",
        "decisions": [],
        "qualifying_progress_count": 0,
        "changed_file_count": 0,
    }
    stats.update(mutation)
    result["worker"]["budget"] = stats
    with pytest.raises(ValueError):
        WorkerResult.parse(json.dumps(result["worker"]))
    replies = tmp_path / "replies.json"
    payload = json.loads(replies.read_text())
    payload[record["job_id"]]["result"] = result
    replies.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        tasks.retrieve(record)


# --- submission: the two time choices -----------------------------------------


def test_a_fresh_submit_without_time_flags_chooses_the_approved_policy(tmp_path, monkeypatch):
    project, calls = budgeted_submission(tmp_path, monkeypatch, "fresh-default")

    # The Namespace omits the new optional fields entirely, as parser defaults
    # and older callers do; the handler boundary resolves the approved policy.
    assert tasks.handler(submit_args(project, "fresh-default")) == 0

    request = tasks.read_record("fresh-default")["request"]
    assert request["budget"] == APPROVED
    assert request["timeout"] == APPROVED["initial_seconds"]
    # The outer Weft budget covers the whole finite cap, never the initial alone.
    assert wall_time(calls) == "15420s"


def test_an_explicit_fixed_timeout_stays_a_hard_limit(tmp_path, monkeypatch):
    project, calls = budgeted_submission(tmp_path, monkeypatch, "fixed")

    assert tasks.handler(submit_args(project, "fixed", timeout=1800)) == 0

    request = tasks.read_record("fixed")["request"]
    assert "budget" not in request
    assert request["timeout"] == 1800
    assert wall_time(calls) == "2820s"


def test_an_explicit_budget_persists_its_policy_and_bounds_the_outer_wall_time(
    tmp_path, monkeypatch
):
    project, calls = budgeted_submission(tmp_path, monkeypatch, "budgeted")

    assert (
        tasks.handler(submit_args(project, "budgeted", budget_initial=1200, budget_cap=6000)) == 0
    )

    request = tasks.read_record("budgeted")["request"]
    assert request["budget"] == {
        "initial_seconds": 1200,
        "extension_seconds": 1800,
        "progress_window_seconds": 900,
        "max_seconds": 6000,
    }
    assert request["timeout"] == 1200
    assert wall_time(calls) == "7020s"


def test_a_cap_smaller_than_initial_plus_extension_is_valid_and_clips_renewal(
    tmp_path, monkeypatch
):
    project, calls = budgeted_submission(tmp_path, monkeypatch, "clipped")

    assert tasks.handler(submit_args(project, "clipped", budget_initial=2000, budget_cap=2500)) == 0

    request = tasks.read_record("clipped")["request"]
    policy = RenewableBudget(**request["budget"])
    now = [0.0]
    clock = BudgetClock(policy, clock=lambda: now[0])
    now[0] = 1999.0
    clock.progress()
    now[0] = 2000.0
    assert clock.remaining() == 500.0
    now[0] = 2500.0
    assert clock.remaining() == 0.0
    assert clock.termination_reason == "absolute_cap"
    assert wall_time(calls) == "3520s"


def test_a_budget_without_an_explicit_cap_defaults_to_the_finite_ceiling(tmp_path, monkeypatch):
    project, calls = budgeted_submission(tmp_path, monkeypatch, "capped")

    assert tasks.handler(submit_args(project, "capped", budget_initial=1200)) == 0

    request = tasks.read_record("capped")["request"]
    assert request["budget"]["max_seconds"] == 14400
    assert wall_time(calls) == "15420s"


def test_an_explicit_cap_with_a_fixed_timeout_refuses_before_dispatch(
    tmp_path, monkeypatch, capsys
):
    project, calls = budgeted_submission(tmp_path, monkeypatch, "refused")

    assert tasks.handler(submit_args(project, "refused", timeout=1800, budget_cap=6000)) == 1

    assert "--budget-cap requires --budget-initial" in capsys.readouterr().err
    assert not (tasks.state_root() / "refused").exists()
    assert not calls.exists()


# --- persistence: the policy object is validated before anything is sent ------


@pytest.mark.parametrize(
    "budget",
    [
        {"initial_seconds": 3600},
        {**APPROVED, "unexpected_field": 1},
        {**APPROVED, "initial_seconds": True},
        {**APPROVED, "max_seconds": "14400"},
        {**APPROVED, "max_seconds": float("nan")},
        {**APPROVED, "max_seconds": float("inf")},
        {**APPROVED, "initial_seconds": 0},
        {**APPROVED, "initial_seconds": 7201},
        {**APPROVED, "max_seconds": 14401},
        {**APPROVED, "extension_seconds": 900},
        {**APPROVED, "progress_window_seconds": 1800},
        {**APPROVED, "max_seconds": 1799},
        "renew forever",
    ],
)
def test_malformed_budgets_refuse_before_dispatch(tmp_path, monkeypatch, budget):
    project, calls = budgeted_submission(tmp_path, monkeypatch, "refused")

    with pytest.raises(TaskError, match="budget"):
        tasks.submit(
            project,
            "Fix hello",
            host="studio",
            selector=SELECTOR,
            timeout=3600,
            check=[],
            budget=budget,
            identity="refused",
        )

    # Nothing persisted, nothing censused, nothing sent.
    assert not (tasks.state_root() / "refused").exists()
    assert not calls.exists()


def test_a_budget_inconsistent_with_its_timeout_refuses(tmp_path, monkeypatch):
    project, calls = budgeted_submission(tmp_path, monkeypatch, "conflict")

    with pytest.raises(TaskError, match="initial_seconds must equal the retained timeout"):
        tasks.submit(
            project,
            "Fix hello",
            host="studio",
            selector=SELECTOR,
            timeout=1800,
            check=[],
            budget=dict(APPROVED),
            identity="conflict",
        )

    assert not (tasks.state_root() / "conflict").exists()
    assert not calls.exists()


# --- resubmission: a stored request keeps its own time meaning ----------------


def test_a_resubmitted_attempt_keeps_its_retained_time_choice(task_factory, tmp_path, monkeypatch):
    from test_task_processing import FakeWeft, abandon

    budgeted, _ = task_factory("budget-retry")
    budgeted["request"]["timeout"] = 3600
    budgeted["request"]["budget"] = dict(APPROVED)
    payload = tasks.state_root() / "budget-retry" / "request.json"
    tasks.save(payload, budgeted["request"])
    budgeted["request_sha256"] = digest(payload.read_bytes())
    tasks.save(tasks.state_root() / "budget-retry" / "task.json", budgeted)
    fixed, _ = task_factory("fixed-retry")

    fake = FakeWeft()
    abandon(fake, budgeted)
    abandon(fake, fixed)
    monkeypatch.setattr(tasks, "run", fake)

    assert tasks.handler(argparse.Namespace(action="resume", id="budget-retry")) == 0
    assert tasks.handler(argparse.Namespace(action="resume", id="fixed-retry")) == 0

    budgeted_run = next(call for call in fake.calls if "budget-retry-2" in call)
    assert budgeted_run[budgeted_run.index("--wall-time") + 1] == "15420s"
    fixed_run = next(call for call in fake.calls if "fixed-retry-2" in call)
    assert fixed_run[fixed_run.index("--wall-time") + 1] == "1050s"


# --- continuation: a fresh explicit choice, nothing inherited -----------------


def test_a_continuation_takes_a_fresh_explicit_budget_and_changes_nothing_else(
    task_factory, tmp_path, monkeypatch, capsys
):
    from test_task_continuation import capture_project

    capture_project(tmp_path, monkeypatch)
    parent, result = task_factory("budget-parent", "validation_failed")

    child = continue_args("budget-parent", budget_initial=1500, budget_cap=6000, id="budget-child")
    assert tasks.handler(child) == 0

    row = json.loads(capsys.readouterr().out)
    assert (row["id"], row["status"], row["parent_id"]) == (
        "budget-child",
        "queued",
        "budget-parent",
    )
    request = tasks.read_record("budget-child")["request"]
    assert request["budget"] == {
        "initial_seconds": 1500,
        "extension_seconds": 1800,
        "progress_window_seconds": 900,
        "max_seconds": 6000,
    }
    assert request["timeout"] == 1500
    # The fixed-timeout parent's seed, check, baseline, and lineage are
    # unchanged; none of the parent's time choice is inherited.
    assert "budget" not in parent["request"]
    assert request["seed"] == result["changes"]
    assert request["check"] == parent["request"]["check"]
    assert request["baseline"] == parent["request"]["baseline"]
    assert request["continuation"]["parent_id"] == "budget-parent"
    assert tasks.read_record("budget-parent") == parent
    # The child's outer Weft budget covers its own cap.
    assert json_wall_time(tmp_path / "weft-calls") == "7020s"


def test_a_continuation_with_an_explicit_fixed_timeout_stays_fixed(
    task_factory, tmp_path, monkeypatch, capsys
):
    from test_task_continuation import capture_project

    capture_project(tmp_path, monkeypatch)
    task_factory("fixed-parent", "validation_failed")

    assert tasks.handler(continue_args("fixed-parent", timeout=120, id="fixed-child")) == 0

    request = tasks.read_record("fixed-child")["request"]
    assert "budget" not in request
    assert request["timeout"] == 120
    assert json_wall_time(tmp_path / "weft-calls") == "1140s"


def test_a_malformed_continuation_budget_creates_no_child(
    task_factory, tmp_path, monkeypatch, capsys
):
    from test_task_continuation import capture_project

    capture_project(tmp_path, monkeypatch)
    task_factory("badbudget-parent", "validation_failed")

    refused = continue_args("badbudget-parent", timeout=600, budget_cap=6000, id="no-child")
    assert tasks.handler(refused) == 1

    assert "--budget-cap requires --budget-initial" in capsys.readouterr().err
    assert not (tasks.state_root() / "no-child").exists()


def test_the_worker_refuses_a_malformed_budget_before_generation(tmp_path, monkeypatch):
    from test_tasks import proposal

    request, _ = proposal(tmp_path)
    request.update(
        id="worker-refusal",
        brief="Edit hello.py",
        selector=SELECTOR,
        timeout=3600,
        check=[],
        budget={**APPROVED, "max_seconds": 60},
    )

    def unexpected_generation(**kwargs):
        raise AssertionError("a malformed budget reached the writer")

    monkeypatch.setattr(task_worker, "execute_worker", unexpected_generation)
    with pytest.raises(TaskError):
        task_worker.execute(request, tmp_path)
    assert (tmp_path / "hello.py").read_text() == "print('before')\n"


def test_a_malformed_budget_publishes_a_retrievable_failed_result(tmp_path, monkeypatch):
    from test_tasks import proposal

    request, _ = proposal(tmp_path)
    request.update(
        schema_version=SCHEMA,
        id="budget-refused",
        brief="Edit hello.py",
        selector=SELECTOR,
        timeout=30,
        check=[],
        execution_sha256=source_sha256(),
        offload_sha256=package_source_sha256(),
        budget={**APPROVED, "initial_seconds": 7201},
    )
    payload = tmp_path / "payload"
    payload.mkdir()
    data = json.dumps(request).encode()
    (payload / "coding-task").write_bytes(data)
    monkeypatch.setenv("WEFT_PAYLOAD_DIR", str(payload))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["worker", "--request-sha256", digest(data), "--task-id", request["id"]]
    )

    def unexpected_generation(**kwargs):
        raise AssertionError("a malformed budget reached the writer")

    monkeypatch.setattr(task_worker, "execute_worker", unexpected_generation)
    with pytest.raises(SystemExit) as failure:
        task_worker.main()
    assert failure.value.code == 1

    result = json.loads((tmp_path / result_path(request["id"])).read_text())
    assert result["status"] == "failed"
    assert result["worker"] is None
    assert "initial_seconds" in result["report"]
