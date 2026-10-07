"""Task disposition acknowledgment, retries, and read-only observation."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_offload import tasks
from agent_offload.task_protocol import TaskError

pytest_plugins = ["test_task_outcomes"]


class FakeWeft:
    """An in-process weft that records every call and can refuse operations.

    It answers only the subcommands the task lane issues. Cancelling a job
    that already ended is refused, as Weft refuses it, so a terminal job has
    to be disposed of rather than cancelled. Tests inject refusals and an
    unusable ledger to drive the failure paths.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.jobs: dict[str, dict] = {}
        self.processed: set[str] = set()
        self.refused: set[str] = set()
        self.recorded: dict[str, str] = {}
        self.cancel_leaves_running = False
        self.broken = False
        self.before_mark: Callable[[str], None] | None = None

    def __call__(self, argv: list[str], *, cwd: Path | None = None, timeout: float = 60) -> str:
        assert argv[0] == "weft"
        argv = argv[1:]
        self.calls.append(list(argv))
        if self.broken:
            raise TaskError("weft: the ledger is unreadable")
        head, rest = argv[0], argv[1:]
        if head == "job":
            return self.job(rest)
        if head == "cancel":
            return self.cancel(rest[0])
        if head == "artifact":
            return self.artifact(rest)
        if head == "run":
            return self.submit_receipt(rest)
        raise TaskError(f"fake weft cannot answer {argv}")

    def entry(self, job_id: str) -> dict:
        entry = self.jobs.get(job_id)
        if entry is None:
            raise TaskError(f"fake weft has no job {job_id}")
        return entry

    def job(self, rest: list[str]) -> str:
        if rest[0] == "inspect":
            return json.dumps(self.entry(rest[1])["job"])
        if rest[0] == "log":
            return self.entry(rest[1]).get("log", "")
        if rest[0] == "mark-processed":
            job_id = rest[1]
            self.entry(job_id)
            if "mark-processed" in self.refused:
                raise TaskError("weft: the ledger refuses the processed transition")
            if self.before_mark is not None:
                self.before_mark(job_id)
            self.processed.add(job_id)
            return ""
        if rest[0] == "lookup":
            key = rest[rest.index("--idempotency-key") + 1]
            job_id = self.recorded.get(key)
            answer = {
                "api_version": "weft.job.lookup.v1",
                "idempotency_key": key,
                "found": job_id is not None,
            }
            if job_id:
                answer["receipt"] = {
                    "api_version": "weft.run.receipt.v1",
                    "job_id": job_id,
                    "placement_decision": "accepted_immediately",
                    "accepted_immediately": True,
                    "deduplicated": False,
                    "selected_host": "studio",
                    "source_pin": "pin",
                    "idempotency_key": key,
                }
            return json.dumps(answer)
        raise TaskError(f"fake weft cannot answer job {rest}")

    def cancel(self, job_id: str) -> str:
        entry = self.entry(job_id)
        if entry["job"]["status"] in tasks.TERMINAL_JOB_STATUSES:
            raise TaskError(f"weft: job {job_id} already ended and cannot be cancelled")
        if "cancel" in self.refused:
            raise TaskError("weft: the daemon cannot be reached")
        if not self.cancel_leaves_running:
            entry["job"]["status"] = "cancelled"
        return ""

    def artifact(self, rest: list[str]) -> str:
        entry = self.entry(rest[1])
        if "result" not in entry:
            raise TaskError(f"fake weft has no result for {rest[1]}")
        return entry["result"]

    def submit_receipt(self, argv: list[str]) -> str:
        key = argv[argv.index("--idempotency-key") + 1]
        return json.dumps(
            {
                "api_version": "weft.run.receipt.v1",
                "job_id": "wj-next",
                "placement_decision": "accepted_immediately",
                "accepted_immediately": True,
                "deduplicated": False,
                "selected_host": "studio",
                "source_pin": "pin",
                "idempotency_key": key,
            }
        )

    def steps(self) -> list[str]:
        return [call[1] if call[0] == "job" else call[0] for call in self.calls]

    def named_mark_processed(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["job", "mark-processed"]]


@pytest.fixture
def weft(monkeypatch):
    fake = FakeWeft()
    monkeypatch.setattr(tasks, "run", fake)
    return fake


def publish(weft, record, result, *, status="completed"):
    """Serve the task's published result and job state from the fake."""
    weft.jobs[record["job_id"]] = {
        "job": {"id": record["job_id"], "status": status},
        "result": json.dumps(result),
        "log": "task worker finished",
    }
    weft.recorded[record["id"]] = record["job_id"]


def abandon(weft, record, *, status="failed"):
    """Serve a job that ended without publishing any result."""
    weft.jobs[record["job_id"]] = {
        "job": {
            "id": record["job_id"],
            "status": status,
            "exit_code": 1,
            "publication": {"required_artifacts_state": "unknown"},
        },
        "log": "agent-offload-task-worker: installed source digest does not match",
    }


def task(action, identity):
    return tasks.handler(argparse.Namespace(action=action, id=identity))


def test_apply_acknowledges_only_after_the_edits_are_durable(task_factory, weft, capsys):
    record, result = task_factory("ack-apply")
    publish(weft, record, result)

    def assert_application_is_durable(job_id):
        assert job_id == record["job_id"]
        retained = tasks.read_record(record["id"])
        assert retained["status"] == "applied" and retained["applied_at"]
        assert not retained.get("processed_at")
        assert (Path(record["project"]) / "hello.py").read_text() == "after\n"

    weft.before_mark = assert_application_is_durable

    assert task("apply", record["id"]) == 0

    assert (Path(record["project"]) / "hello.py").read_text() == "after\n"
    retained = tasks.read_record(record["id"])
    assert retained["applied_at"] and retained["processed_at"]
    assert json.loads(capsys.readouterr().out)["processed_at"] == retained["processed_at"]
    assert weft.processed == {record["job_id"]}
    assert weft.steps().index("mark-processed") > weft.steps().index("artifact")


def test_a_failed_application_acknowledges_nothing(task_factory, weft):
    record, result = task_factory("ack-refused-apply")
    publish(weft, record, result)
    (Path(record["project"]) / "hello.py").write_text("edited locally\n")

    assert task("apply", record["id"]) == 1

    assert weft.processed == set()
    assert "mark-processed" not in weft.steps()
    retained = tasks.read_record(record["id"])
    assert "applied_at" not in retained and "processed_at" not in retained


def test_an_apply_of_a_still_running_job_stays_owed_until_weft_finishes(task_factory, weft):
    # The result artifact can be published before Weft closes the job, so an
    # application can race the job's completion; the acknowledgment must not.
    record, result = task_factory("ack-running")
    publish(weft, record, result, status="running")

    assert task("apply", record["id"]) == 1

    assert (Path(record["project"]) / "hello.py").read_text() == "after\n"
    retained = tasks.read_record(record["id"])
    assert retained["applied_at"] and "processed_at" not in retained
    assert "mark-processed" not in weft.steps()

    weft.jobs[record["job_id"]]["job"]["status"] = "completed"
    seen = len(weft.calls)
    assert task("apply", record["id"]) == 0
    assert weft.processed == {record["job_id"]}
    # The retry owed only the acknowledgment: no revalidation, no project
    # writes, and no submission.
    assert weft.steps()[seen:] == ["inspect", "mark-processed"]


def test_a_refused_acknowledgment_is_owed_and_retryable_without_reapplying(
    task_factory, weft, capsys
):
    record, result = task_factory("ack-owed")
    publish(weft, record, result)
    weft.refused.add("mark-processed")

    assert task("apply", record["id"]) == 1
    assert "mark-processed" in capsys.readouterr().err
    assert (Path(record["project"]) / "hello.py").read_text() == "after\n"
    retained = tasks.read_record(record["id"])
    assert retained["applied_at"] and "processed_at" not in retained

    weft.refused.clear()
    (Path(record["project"]) / "hello.py").write_text("edited after application\n")
    seen = len(weft.calls)
    assert task("apply", record["id"]) == 0
    assert weft.steps()[seen:] == ["inspect", "mark-processed"]
    assert (Path(record["project"]) / "hello.py").read_text() == "edited after application\n"
    assert tasks.read_record(record["id"])["processed_at"]


def test_an_apply_after_a_completed_acknowledgment_refuses_and_remarks_nothing(
    task_factory, weft, capsys
):
    record, result = task_factory("ack-done")
    publish(weft, record, result)
    assert task("apply", record["id"]) == 0

    seen = len(weft.calls)
    assert task("apply", record["id"]) == 1
    assert "already applied" in capsys.readouterr().err

    assert weft.calls[seen:] == []
    assert weft.named_mark_processed() == [["job", "mark-processed", record["job_id"]]]
    assert (Path(record["project"]) / "hello.py").read_text() == "after\n"


def test_status_retrieve_diff_and_list_never_acknowledge(task_factory, weft, capsys):
    record, result = task_factory("read-only")
    publish(weft, record, result)
    for action in ("status", "retrieve", "list"):
        assert tasks.handler(argparse.Namespace(action=action, id=record["id"])) == 0
        capsys.readouterr()
    assert task("diff", record["id"]) == 0
    capsys.readouterr()

    assert "mark-processed" not in weft.steps()
    assert weft.processed == set()
    assert "processed_at" not in tasks.read_record(record["id"])


@pytest.mark.parametrize("status", ["completed", "canceled"])
def test_cancel_disposes_an_already_terminal_job_without_weft_cancel(
    task_factory, weft, capsys, status
):
    record, result = task_factory(f"cancel-{status}")
    publish(weft, record, result, status=status)

    assert task("cancel", record["id"]) == 0

    row = json.loads(capsys.readouterr().out)
    assert row["status"] == status
    assert weft.processed == {record["job_id"]}
    assert "cancel" not in weft.steps()
    assert tasks.read_record(record["id"])["processed_at"]
    # A repeated cancel is idempotent: the owed disposition is already cleared.
    assert task("cancel", record["id"]) == 0
    assert weft.named_mark_processed() == [["job", "mark-processed", record["job_id"]]]


def test_cancel_confirms_the_cancellation_before_acknowledging(task_factory, weft, capsys):
    record, result = task_factory("cancel-running")
    publish(weft, record, result, status="queued")

    assert task("cancel", record["id"]) == 0

    row = json.loads(capsys.readouterr().out)
    assert row["status"] == "cancelled"
    assert weft.jobs[record["job_id"]]["job"]["status"] == "cancelled"
    assert weft.processed == {record["job_id"]}
    steps = weft.steps()
    assert steps.index("cancel") < steps.index("mark-processed")
    # The acknowledgment observed the cancellation before requesting it.
    assert steps[steps.index("mark-processed") - 1] == "inspect"


def test_cancel_never_marks_a_job_weft_still_runs(task_factory, weft):
    record, result = task_factory("cancel-stuck")
    publish(weft, record, result, status="running")
    weft.cancel_leaves_running = True

    assert task("cancel", record["id"]) == 1

    assert "mark-processed" not in weft.steps()
    assert weft.processed == set()
    assert "processed_at" not in tasks.read_record(record["id"])


def test_a_failed_cancellation_or_observation_acknowledges_nothing(task_factory, weft):
    record, result = task_factory("cancel-broken")
    publish(weft, record, result, status="running")

    weft.refused.add("cancel")
    assert task("cancel", record["id"]) == 1
    assert "mark-processed" not in weft.steps()

    weft.refused.clear()
    weft.broken = True
    assert task("cancel", record["id"]) == 1
    assert "mark-processed" not in weft.steps()
    assert weft.processed == set()
    assert "processed_at" not in tasks.read_record(record["id"])


def test_resume_disposes_the_dead_attempt_before_resubmitting(task_factory, weft, capsys):
    record, _ = task_factory("retry-ack")
    abandon(weft, record)

    assert task("resume", record["id"]) == 0

    row = json.loads(capsys.readouterr().out)
    assert row["job_id"] == "wj-next"
    assert row["prior_job_ids"] == [record["job_id"]]
    assert weft.processed == {record["job_id"]}
    steps = weft.steps()
    assert steps.index("mark-processed") < steps.index("run")
    submitted = next(call for call in weft.calls if call[0] == "run")
    assert submitted[submitted.index("--idempotency-key") + 1] == f"{record['id']}-2"
    retained = tasks.read_record(record["id"])
    # The acknowledgment described the archived attempt; the new one starts
    # unacknowledged, so its own disposition can still be owed.
    assert "processed_at" not in retained
    assert retained["attempt"] == 2


def test_a_refused_disposition_keeps_the_dead_identity_and_submits_nothing(
    task_factory, weft, capsys
):
    record, _ = task_factory("retry-owed")
    abandon(weft, record)
    weft.refused.add("mark-processed")

    assert task("resume", record["id"]) == 1
    assert "mark-processed" in capsys.readouterr().err
    retained = tasks.read_record(record["id"])
    assert retained["job_id"] == record["job_id"]
    assert "attempt" not in retained
    assert not retained.get("prior_job_ids")
    assert not any(call[0] == "run" for call in weft.calls)
    assert weft.processed == set()

    weft.refused.clear()
    seen = len(weft.calls)
    assert task("resume", record["id"]) == 0
    steps = weft.steps()[seen:]
    assert steps.count("run") == 1
    assert steps.index("mark-processed") < steps.index("run")
    assert weft.processed == {record["job_id"]}
    assert tasks.read_record(record["id"])["prior_job_ids"] == [record["job_id"]]


def test_recovering_an_accepted_submission_acknowledges_nothing(task_factory, weft, capsys):
    record, result = task_factory("lost-receipt")
    publish(weft, record, result)
    accepted = record["job_id"]
    retained = tasks.read_record(record["id"])
    retained.update(job_id=None, status="submission_unknown", submission_started=True)
    tasks.save(tasks.state_root() / record["id"] / "task.json", retained)

    assert task("resume", record["id"]) == 0

    row = json.loads(capsys.readouterr().out)
    assert row["job_id"] == accepted
    assert row["status"] == "queued"
    assert not any(call[0] == "run" for call in weft.calls)
    assert "mark-processed" not in weft.steps()
    assert weft.processed == set()


def test_an_application_acknowledges_only_its_own_task(task_factory, weft):
    first, first_result = task_factory("ack-first")
    second, second_result = task_factory("ack-second")
    publish(weft, first, first_result)
    publish(weft, second, second_result)

    assert task("apply", first["id"]) == 0

    assert weft.processed == {first["job_id"]}
    assert weft.named_mark_processed() == [["job", "mark-processed", first["job_id"]]]
    assert tasks.read_record(second["id"]).get("processed_at") is None
