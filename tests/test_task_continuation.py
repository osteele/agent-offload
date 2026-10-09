"""Explicit continuation of failed tasks from their retained partial edits."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_execution.identity import source_sha256
from agent_execution.omp_execution import (
    OMP_EXECUTION_SCHEMA,
    OMP_SDK_VERSION,
    OMP_WRITE_TOOLS,
    omp_transcript,
)
from agent_execution.worker import WORKER_PROTOCOL_VERSION, HarnessOutcome, WorkerResult
from test_task_outcomes import task_factory as parent_task_factory

from agent_offload import task_worker, tasks, worker_build
from agent_offload.protocol import package_source_sha256
from agent_offload.task_protocol import (
    SCHEMA,
    TaskError,
    canonical_digest,
    digest,
    encode,
    manifest,
)

task_factory = parent_task_factory

# --- helpers -----------------------------------------------------------------


def continue_args(parent, *, timeout=120, prompt=None, brief=None, id=None):
    return argparse.Namespace(
        action="continue", parent=parent, timeout=timeout, prompt=prompt, brief=brief, id=id
    )


def ordinary_names(root: Path) -> list[str]:
    """The snapshot listing the real census would take, without rg/weft on PATH."""
    return [
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    ]


def capture_project(tmp_path, monkeypatch):
    """Serve the ordinary snapshot listing locally; the drift check stays real."""
    monkeypatch.setattr(tasks, "snapshot_names", ordinary_names)


def serve(tmp_path, record, result):
    replies = tmp_path / "replies.json"
    payloads = json.loads(replies.read_text())
    payloads[record["job_id"]]["result"] = result
    replies.write_text(json.dumps(payloads))


def publish_child(tmp_path, job_id, result, *, status="completed"):
    replies = tmp_path / "replies.json"
    payloads = json.loads(replies.read_text())
    payloads[job_id] = {"job": {"id": job_id, "status": status}, "result": result}
    replies.write_text(json.dumps(payloads))


def processed_jobs(tmp_path) -> list[str]:
    file = tmp_path / "processed-jobs"
    return file.read_text().splitlines() if file.exists() else []


def weft_runs(tmp_path) -> list[list[str]]:
    file = tmp_path / "weft-calls"
    if not file.exists():
        return []
    calls = [json.loads(line) for line in file.read_text().splitlines()]
    return [call for call in calls if call[0] == "run"]


def rewrite_request(tmp_path, record, result, **fields):
    """Retain an amended request and keep its served result digest-consistent."""
    record["request"].update(fields)
    if "check" in fields and isinstance(result.get("validation"), dict):
        result["validation"]["command"] = list(fields["check"])
    payload = tasks.state_root() / record["id"] / "request.json"
    tasks.save(payload, record["request"])
    record["request_sha256"] = digest(payload.read_bytes())
    tasks.save(tasks.state_root() / record["id"] / "task.json", record)
    result["request_sha256"] = record["request_sha256"]
    serve(tmp_path, record, result)


def synthetic_output(root: Path, request: dict) -> str:
    header = {
        "type": "execution",
        "schema_version": OMP_EXECUTION_SCHEMA,
        "sdk_version": OMP_SDK_VERSION,
        "harness": "omp",
        "selector": request["selector"],
        "tool_policy": "workspace-write-no-shell",
        "tools": list(OMP_WRITE_TOOLS),
        "session_id": "synthetic-continuation-session",
        "cwd": str(root),
        "prompt_sha256": digest(request["brief"].encode()),
        "execution_id": "00000000-0000-0000-0000-000000000002",
    }
    events = [
        header,
        {"type": "session", "version": 3, "id": header["session_id"], "cwd": str(root)},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "provider": "openai-codex",
                "model": "gpt-6-astra",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "Synthetic continuation"}],
            },
        },
        {"type": "agent_end", "messages": []},
        {"type": "execution_end", "session_id": header["session_id"]},
    ]
    return "\n".join(json.dumps(event) for event in events)


def run_child_worker(tmp_path, monkeypatch, record, *, edit, expect_status=None):
    """Run the real worker against a disposable copy and publish its result.

    The seed is applied only inside the copy, mirroring the remote snapshot;
    the live project is asserted untouched by the caller.
    """
    project = Path(record["project"])
    snapshot = tmp_path / "snapshot"
    shutil.copytree(project, snapshot)
    request = {**record["request"], "request_sha256": record["request_sha256"]}

    def writer(**kwargs):
        root = Path(kwargs["cwd"])
        edit(root)
        output = synthetic_output(root, request)
        return WorkerResult(
            model_call_id=request["id"],
            provider="omp",
            status="completed",
            worker_version="test",
            worker_protocol_version=WORKER_PROTOCOL_VERSION,
            worker_source_sha256=request["execution_sha256"],
            ctx_version="",
            worker_cwd=str(root),
            started_at=1,
            completed_at=2,
            model_call_started=True,
            session_id="synthetic-continuation-session",
            harness=HarnessOutcome(0, output, ""),
            prompt_sha256=digest(request["brief"].encode()),
            omp_evidence=omp_transcript(output),
        )

    monkeypatch.setattr(task_worker, "execute_worker", writer)
    result = task_worker.execute(request, snapshot)
    if expect_status is not None:
        assert result["status"] == expect_status
    publish_child(tmp_path, record["job_id"], result)
    return result


def seeded_request(root: Path, seed: list[dict], *, extra_baseline: list[str] = ()) -> dict:
    (root / "hello.py").write_text("print('before')\n")
    (root / "hello.py").chmod(0o644)
    return {
        "schema_version": SCHEMA,
        "id": "seeded",
        "brief": "Edit hello.py",
        "selector": "openai-codex/gpt-6-astra",
        "timeout": 30,
        "check": [],
        "baseline": manifest(root, ["hello.py", *extra_baseline]),
        "carried": {},
        "execution_sha256": source_sha256(),
        "offload_sha256": package_source_sha256(),
        "request_sha256": "a" * 64,
        "seed": seed,
    }


def stub_writer(monkeypatch, edit):
    """Replace only the model launch; census, seeding, checks stay real."""

    def write(**kwargs):
        edit(Path(kwargs["cwd"]))
        return SimpleNamespace(status="completed", harness=SimpleNamespace(stdout=""), to_dict=dict)

    monkeypatch.setattr(task_worker, "execute_worker", write)
    monkeypatch.setattr(task_worker, "omp_final_text", lambda output: "done")


HELLO_SEED = [
    {
        "path": "hello.py",
        "before": encode(b"print('before')\n"),
        "after": encode(b"seeded\n"),
        "before_mode": 0o644,
        "after_mode": 0o644,
    }
]

ADDITION_SEED = [
    {
        "path": "new.py",
        "before": None,
        "after": encode(b"seeded new\n"),
        "before_mode": None,
        "after_mode": 0o644,
    }
]


# --- worker: seed application and cumulative diffs ---------------------------


def test_seed_is_applied_at_worker_entry_and_changes_span_baseline_to_final(tmp_path, monkeypatch):
    # Guard: the worker materializes the parent's partial edits in its
    # disposable snapshot before generation, and the returned diff is
    # cumulative from the original baseline, not a delta from the seed.
    seed = HELLO_SEED + ADDITION_SEED
    request = seeded_request(tmp_path, seed)

    def finish(root):
        assert (root / "hello.py").read_text() == "seeded\n"
        assert (root / "new.py").read_text() == "seeded new\n"
        (root / "hello.py").write_text("final\n")

    stub_writer(monkeypatch, finish)
    result = task_worker.execute(request, tmp_path)
    assert result["status"] == "completed"
    assert result["changes"] == [
        {
            "path": "hello.py",
            "before": encode(b"print('before')\n"),
            "after": encode(b"final\n"),
            "before_mode": 0o644,
            "after_mode": 0o644,
        },
        {
            "path": "new.py",
            "before": None,
            "after": encode(b"seeded new\n"),
            "before_mode": None,
            "after_mode": 0o644,
        },
    ]


def test_a_seed_edit_the_writer_reverts_vanishes_from_the_diff(tmp_path, monkeypatch):
    # Guard: reverted seed edits are not reported as changes; the diff is
    # against the original baseline, so a full revert is no change at all.
    request = seeded_request(tmp_path, HELLO_SEED)
    stub_writer(monkeypatch, lambda root: (root / "hello.py").write_text("print('before')\n"))
    result = task_worker.execute(request, tmp_path)
    assert result["changes"] == []


def test_seed_mode_changes_and_deletions_reach_the_final_diff(tmp_path, monkeypatch):
    # Guard: mode changes and deletions of seeded state are represented in the
    # baseline -> final diff exactly like ordinary writer edits.
    (tmp_path / "helper.py").write_text("helper\n")
    (tmp_path / "helper.py").chmod(0o644)
    seed = [
        {
            "path": "hello.py",
            "before": encode(b"print('before')\n"),
            "after": encode(b"print('before')\n"),
            "before_mode": 0o644,
            "after_mode": 0o755,
        },
        {
            "path": "helper.py",
            "before": encode(b"helper\n"),
            "after": None,
            "before_mode": 0o644,
            "after_mode": None,
        },
    ]
    request = seeded_request(tmp_path, seed, extra_baseline=["helper.py"])
    stub_writer(monkeypatch, lambda root: None)
    result = task_worker.execute(request, tmp_path)
    assert {change["path"]: change for change in result["changes"]} == {
        "helper.py": {
            "path": "helper.py",
            "before": encode(b"helper\n"),
            "after": None,
            "before_mode": 0o644,
            "after_mode": None,
        },
        "hello.py": {
            "path": "hello.py",
            "before": encode(b"print('before')\n"),
            "after": encode(b"print('before')\n"),
            "before_mode": 0o644,
            "after_mode": 0o755,
        },
    }


def test_a_malformed_seed_is_refused_before_any_mutation(tmp_path, monkeypatch):
    # Guard: a seed that disagrees with the retained baseline is rejected
    # before a single byte is written and before the model is launched.
    seed = [
        {
            "path": "hello.py",
            "before": encode(b"invented baseline\n"),
            "after": encode(b"seeded\n"),
            "before_mode": 0o644,
            "after_mode": 0o644,
        }
    ]
    request = seeded_request(tmp_path, seed)

    def unexpected_generation(**kwargs):
        raise AssertionError("a malformed seed reached the model launch")

    monkeypatch.setattr(task_worker, "execute_worker", unexpected_generation)
    with pytest.raises(TaskError, match="baseline bytes"):
        task_worker.execute(request, tmp_path)
    assert (tmp_path / "hello.py").read_text() == "print('before')\n"


@pytest.mark.parametrize("seed", [None, {}, False, ""])
def test_falsey_malformed_seed_cannot_be_treated_as_an_empty_patch(tmp_path, monkeypatch, seed):
    request = seeded_request(tmp_path, HELLO_SEED)
    request["seed"] = seed
    stub_writer(monkeypatch, lambda root: pytest.fail("invalid seed reached generation"))
    with pytest.raises(TaskError):
        task_worker.execute(request, tmp_path)
    assert (tmp_path / "hello.py").read_text() == "print('before')\n"


def test_a_failed_continuation_still_retains_cumulative_edits(tmp_path, monkeypatch):
    # Guard: a harness failure after seeding publishes a retrievable result
    # whose changes remain the full baseline -> final state.
    request = seeded_request(tmp_path, HELLO_SEED + ADDITION_SEED)
    monkeypatch.setattr(
        task_worker,
        "execute_worker",
        lambda **kwargs: SimpleNamespace(
            status="harness_failed", harness=None, failure="harness exited 1", to_dict=dict
        ),
    )
    result = task_worker.execute(request, tmp_path)
    assert result["status"] == "failed"
    assert [change["path"] for change in result["changes"]] == ["hello.py", "new.py"]
    assert result["changes"][0]["after"] == encode(b"seeded\n")


# --- client: eligibility, drift refusal, provenance, custody ------------------


def test_continue_seeds_a_child_from_the_retained_parent_edits(
    task_factory, tmp_path, monkeypatch, capsys
):
    # Guard: the child is an ordinary durable task carrying the parent's
    # original baseline, validated seed, and task/job/request/result
    # provenance by digest; the parent is preserved and never acknowledged.
    capture_project(tmp_path, monkeypatch)
    parent, result = task_factory("cont-parent", "validation_failed")
    assert tasks.handler(continue_args("cont-parent", id="cont-child")) == 0
    child = json.loads(capsys.readouterr().out)
    assert (child["id"], child["status"], child["parent_id"]) == (
        "cont-child",
        "queued",
        "cont-parent",
    )
    record = tasks.read_record("cont-child")
    request = record["request"]
    assert request["baseline"] == parent["request"]["baseline"]
    assert request["carried"] == parent["request"]["carried"]
    assert request["seed"] == result["changes"]
    assert request["check"] == parent["request"]["check"]
    assert request["selector"] == parent["request"]["selector"]
    assert request["timeout"] == 120
    assert request["continuation"] == {
        "parent_id": "cont-parent",
        "parent_job_id": parent["job_id"],
        "parent_request_sha256": parent["request_sha256"],
        "parent_result_sha256": canonical_digest(result),
        "seed_sha256": canonical_digest(result["changes"]),
    }
    assert request["execution_sha256"] == source_sha256()
    assert request["offload_sha256"] == package_source_sha256()
    # The brief preserves the original requirements and flags the seed.
    assert parent["request"]["brief"] in request["brief"]
    # The parent record is byte-identical and nothing acknowledged its job.
    assert tasks.read_record("cont-parent") == parent
    assert parent["job_id"] not in processed_jobs(tmp_path)
    assert [call[call.index("--idempotency-key") + 1] for call in weft_runs(tmp_path)] == [
        "cont-child"
    ]


def test_continue_prints_a_generated_child_identity_like_submit(
    task_factory, tmp_path, monkeypatch, capsys
):
    # Guard: without --id the child gets a fresh generated identity.
    capture_project(tmp_path, monkeypatch)
    task_factory("gen-parent", "validation_failed")
    assert tasks.handler(continue_args("gen-parent")) == 0
    child = json.loads(capsys.readouterr().out)
    assert child["id"] != "gen-parent"
    assert tasks.read_record(child["id"])["request"]["seed"]
    assert child["status"] == "queued"


def test_continue_refuses_a_changed_file_and_a_new_local_file_before_submission(
    task_factory, tmp_path, monkeypatch, capsys
):
    # Guard: the live project must still equal the parent's retained original
    # baseline, including the path set, so local drift and a new file that
    # could collide with a seeded addition are refused before any submission,
    # and nothing local is written.
    capture_project(tmp_path, monkeypatch)
    parent, _ = task_factory("drift-parent", "validation_failed")
    (Path(parent["project"]) / "hello.py").write_text("edited locally\n")
    assert tasks.handler(continue_args("drift-parent", id="drift-child")) == 1
    assert "project changed" in capsys.readouterr().err
    assert not (tasks.state_root() / "drift-child").exists()

    (Path(parent["project"]) / "hello.py").write_text("before\n")
    parent2, result = task_factory("collide-parent", "validation_failed")
    result["changes"] += ADDITION_SEED
    serve(tmp_path, parent2, result)
    (Path(parent2["project"]) / "new.py").write_text("local author\n")
    assert tasks.handler(continue_args("collide-parent", id="collide-child")) == 1
    assert "project changed" in capsys.readouterr().err
    assert not (tasks.state_root() / "collide-child").exists()
    assert (Path(parent2["project"]) / "new.py").read_text() == "local author\n"
    assert [call for call in weft_runs(tmp_path) if "collide-child" in call] == []


@pytest.mark.parametrize(
    "outcome,job_status",
    [("completed", "completed"), ("validation_failed", "running")],
    ids=["completed", "running"],
)
def test_continue_refuses_completed_and_running_parents(
    task_factory, tmp_path, monkeypatch, capsys, outcome, job_status
):
    # Guard: a succeeded task is applied, not continued, and a still-running
    # job has no finished outcome to continue from.
    capture_project(tmp_path, monkeypatch)
    record, _ = task_factory("busy-parent", outcome)
    if job_status != "completed":
        replies = tmp_path / "replies.json"
        payloads = json.loads(replies.read_text())
        payloads[record["job_id"]]["job"]["status"] = job_status
        replies.write_text(json.dumps(payloads))
    assert tasks.handler(continue_args("busy-parent", id="busy-child")) == 1
    error = capsys.readouterr().err
    assert job_status in error or "apply" in error
    assert not (tasks.state_root() / "busy-child").exists()
    assert weft_runs(tmp_path) == []


def test_continue_refuses_a_parent_cancelled_without_a_result(
    task_factory, tmp_path, monkeypatch, capsys
):
    # Guard: a cancelled job that published nothing has no retained edits.
    capture_project(tmp_path, monkeypatch)
    record, _ = task_factory("cancel-parent", "validation_failed")
    replies = tmp_path / "replies.json"
    payloads = json.loads(replies.read_text())
    payloads[record["job_id"]] = {"job": {"id": record["job_id"], "status": "cancelled"}}
    replies.write_text(json.dumps(payloads))
    assert tasks.handler(continue_args("cancel-parent", id="cancel-child")) == 1
    error = capsys.readouterr().err
    assert "no result to continue from" in error and "cancelled" in error
    assert not (tasks.state_root() / "cancel-child").exists()
    assert weft_runs(tmp_path) == []


def test_continue_refuses_a_malformed_parent_result(task_factory, tmp_path, monkeypatch, capsys):
    # Guard: a result that fails validation against the retained dispatch is
    # never promoted into a child's seed.
    capture_project(tmp_path, monkeypatch)
    record, result = task_factory("malformed-parent", "validation_failed")
    result["request_sha256"] = "wrong-dispatch"
    serve(tmp_path, record, result)
    assert tasks.handler(continue_args("malformed-parent", id="malformed-child")) == 1
    error = capsys.readouterr().err
    assert "cannot be continued" in error
    assert not (tasks.state_root() / "malformed-child").exists()
    assert weft_runs(tmp_path) == []


def test_continue_refuses_an_applied_parent_and_an_unknown_identity(
    task_factory, tmp_path, monkeypatch, capsys
):
    # Guard: continuing an applied task could duplicate edits already in the
    # project; an unknown parent has nothing to continue.
    capture_project(tmp_path, monkeypatch)
    task_factory("applied-parent", "validation_failed")
    retained = tasks.read_record("applied-parent")
    retained["applied_at"] = 1.0
    tasks.save(tasks.state_root() / "applied-parent" / "task.json", retained)
    assert tasks.handler(continue_args("applied-parent", id="applied-child")) == 1
    assert "already applied" in capsys.readouterr().err
    assert tasks.handler(continue_args("missing-parent", id="missing-child")) == 1
    assert capsys.readouterr().err
    assert not (tasks.state_root() / "applied-child").exists()
    assert not (tasks.state_root() / "missing-child").exists()
    assert weft_runs(tmp_path) == []


def test_continue_accepts_a_parent_already_acknowledged_with_cancel(
    task_factory, tmp_path, monkeypatch, capsys
):
    # Guard: the real ao21 case — a terminal parent acknowledged with task
    # cancel stays eligible, and continuing it never re-acknowledges or
    # otherwise mutates the parent.
    capture_project(tmp_path, monkeypatch)
    parent, result = task_factory("ack-parent", "validation_failed")
    assert tasks.handler(argparse.Namespace(action="cancel", id="ack-parent")) == 0
    snapshot = tasks.read_record("ack-parent")
    assert snapshot["processed_at"]
    processed = processed_jobs(tmp_path)
    assert processed.count(parent["job_id"]) == 1
    assert tasks.handler(continue_args("ack-parent", id="ack-child")) == 0
    capsys.readouterr()
    assert tasks.read_record("ack-parent") == snapshot
    assert processed_jobs(tmp_path) == processed
    assert tasks.read_record("ack-child")["request"]["seed"] == result["changes"]


@pytest.mark.parametrize("answers_with_current_build", [True, False], ids=["current", "stale"])
def test_continue_pins_the_child_to_the_current_build_not_the_parent_pins(
    task_factory, tmp_path, monkeypatch, capsys, answers_with_current_build, isolated_config
):
    # Guard: an older parent dispatched by a previous build still continues
    # (validated against its own retained request), while the child is pinned
    # to the current build; provisioning against the parent's stale digests
    # refuses instead of queueing a mismatched worker.
    from conftest import REAL_ENSURE_WORKER
    from test_task_processing import FakeWeft

    parent, result = task_factory("old-pin", "validation_failed")
    parent["request"]["offload_sha256"] = "1" * 64
    parent["request"]["execution_sha256"] = "0" * 64
    payload = tasks.state_root() / "old-pin" / "request.json"
    tasks.save(payload, parent["request"])
    parent["request_sha256"] = digest(payload.read_bytes())
    tasks.save(tasks.state_root() / "old-pin" / "task.json", parent)
    result["offload_sha256"] = "1" * 64
    result["request_sha256"] = parent["request_sha256"]
    result["worker"]["worker_source_sha256"] = "0" * 64

    fake = FakeWeft()
    fake.jobs[parent["job_id"]] = {
        "job": {"id": parent["job_id"], "status": "completed"},
        "result": json.dumps(result),
    }
    offload_sha = package_source_sha256() if answers_with_current_build else "1" * 64
    execution_sha = source_sha256() if answers_with_current_build else "0" * 64
    build_id = worker_build.ssh_build_id(package_source_sha256(), source_sha256())
    identity = {
        "build_id": build_id,
        "source_sha256": offload_sha,
        "execution": {"source_sha256": execution_sha},
    }
    bin_dir = "/worker/build/bin"

    def run_provider(argv, **kwargs):
        assert argv[0] == "ssh"
        return f"{bin_dir}\n{json.dumps(identity)}\n".encode()

    isolated_config.write_text('[worker]\nprovider = "agent-host-sync"\n')
    monkeypatch.setattr(worker_build, "run_provider", run_provider)
    monkeypatch.setattr(tasks, "run", fake)
    monkeypatch.setattr(tasks, "ensure_worker", REAL_ENSURE_WORKER)
    monkeypatch.setattr(tasks, "snapshot_names", ordinary_names)
    exit_code = tasks.handler(continue_args("old-pin", id="pin-child"))
    capsys.readouterr()
    if answers_with_current_build:
        assert exit_code == 0
        record = tasks.read_record("pin-child")
        assert record["request"]["execution_sha256"] == source_sha256()
        assert record["request"]["offload_sha256"] == package_source_sha256()
        assert (record["bin_dir"], record["build_id"]) == (bin_dir, build_id)
        assert (
            record["request"]["continuation"]["parent_request_sha256"] == parent["request_sha256"]
        )
    else:
        assert exit_code == 1
        retained = tasks.read_record("pin-child")
        assert retained["job_id"] is None and "bin_dir" not in retained
        assert fake.steps() == ["inspect", "artifact"]


@pytest.mark.parametrize("receipt_recorded", [True, False])
def test_an_interrupted_continuation_keeps_custody_and_resume_binds_one_job(
    task_factory, tmp_path, monkeypatch, capsys, receipt_recorded
):
    # Guard: the child's immutable request (seed and provenance included) is
    # durable before any remote side effect, so an interrupted send recovers
    # through `task resume CHILD` under one idempotency key.
    from test_task_processing import FakeWeft

    parent, result = task_factory("int-parent", "validation_failed")
    fake = FakeWeft()
    fake.jobs[parent["job_id"]] = {
        "job": {"id": parent["job_id"], "status": "completed"},
        "result": json.dumps(result),
    }

    def lose_receipt(argv, **kwargs):
        if argv[:2] == ["weft", "run"]:
            fake.recorded["int-child"] = "wj-next"
            fake.broken = True
        return fake(argv, **kwargs)

    monkeypatch.setattr(tasks, "run", lose_receipt)
    monkeypatch.setattr(tasks, "snapshot_names", ordinary_names)
    assert tasks.handler(continue_args("int-parent", id="int-child")) == 1
    capsys.readouterr()
    retained = tasks.read_record("int-child")
    assert (retained["status"], retained["job_id"]) == ("submission_unknown", None)
    assert retained["submission_started"] is True
    assert retained["request"]["seed"] == result["changes"]
    assert retained["request"]["continuation"]["parent_id"] == "int-parent"

    fake.broken = False
    if not receipt_recorded:
        del fake.recorded["int-child"]
    monkeypatch.setattr(tasks, "run", fake)
    collision = Path(parent["project"]) / "new.py"
    collision.write_text("local work\n")
    exit_code = tasks.handler(argparse.Namespace(action="resume", id="int-child"))
    assert exit_code == (0 if receipt_recorded else 1)
    assert collision.read_text() == "local work\n"
    if not receipt_recorded:
        assert tasks.read_record("int-child") == retained
        assert fake.steps().count("run") == 1
        return
    resumed = json.loads(capsys.readouterr().out)
    assert (resumed["job_id"], resumed["status"], resumed["parent_id"]) == (
        "wj-next",
        "queued",
        "int-parent",
    )
    assert tasks.read_record("int-child")["request"] == retained["request"]
    assert "attempt" not in tasks.read_record("int-child")
    assert fake.steps() == ["inspect", "artifact", "run", "lookup"]
    keys = [call[call.index("--idempotency-key") + 1] for call in fake.calls if call[0] == "run"]
    assert keys == ["int-child"]


@pytest.mark.parametrize("timeout", [0, 7201])
def test_continue_requires_the_explicit_bounded_timeout(
    task_factory, tmp_path, monkeypatch, capsys, timeout
):
    # Guard: the continuation budget is the caller's explicit choice within
    # the standing 1-7200 range; nothing is dispatched without it.
    capture_project(tmp_path, monkeypatch)
    task_factory("timeout-parent", "validation_failed")
    assert tasks.handler(continue_args("timeout-parent", timeout=timeout)) == 1
    assert "timeout" in capsys.readouterr().err
    assert weft_runs(tmp_path) == []


def test_the_continue_parser_takes_the_documented_form():
    # Guard: `task continue PARENT --timeout S [--brief FILE | --prompt TEXT] [--id CHILD]`.
    from agent_offload.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["task", "continue", "p", "--timeout", "600", "--prompt", "finish it", "--id", "c"]
    )
    assert (args.parent, args.timeout, args.prompt, args.id) == ("p", 600.0, "finish it", "c")
    with pytest.raises(SystemExit):
        parser.parse_args(["task", "continue", "p"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["task", "continue", "p", "--timeout", "1", "--brief", "a", "--prompt", "b"]
        )


# --- end-to-end: fresh execution, checks, apply, chaining --------------------


def test_a_continuation_runs_fresh_and_applies_only_the_validated_final_state(
    task_factory, tmp_path, monkeypatch, capsys
):
    # Guard: the child executes fresh on the seeded snapshot, the requested
    # check runs on the final combined state, nothing touches the live project
    # before application, and applying writes the full combined result.
    capture_project(tmp_path, monkeypatch)
    parent, result = task_factory("apply-parent", "validation_failed")
    result["changes"] += ADDITION_SEED
    rewrite_request(
        tmp_path,
        parent,
        result,
        check=[
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "assert Path('hello.py').read_text() == 'final\\n'; "
                "assert Path('new.py').read_text() == 'seeded new\\n'"
            ),
        ],
    )
    assert tasks.handler(continue_args("apply-parent", id="apply-child")) == 0
    capsys.readouterr()
    child = tasks.read_record("apply-child")
    project = Path(parent["project"])
    assert (project / "hello.py").read_text() == "before\n"
    assert not (project / "new.py").exists()

    child_result = run_child_worker(
        tmp_path,
        monkeypatch,
        child,
        edit=lambda root: (root / "hello.py").write_text("final\n"),
        expect_status="completed",
    )
    assert child_result["validation"]["exit_code"] == 0
    assert [(c["path"], c["after"]) for c in child_result["changes"]] == [
        ("hello.py", encode(b"final\n")),
        ("new.py", encode(b"seeded new\n")),
    ]
    # The seed never touched the live project; only application can.
    assert tasks.handler(argparse.Namespace(action="apply", id="apply-child")) == 0
    capsys.readouterr()
    assert (project / "hello.py").read_text() == "final\n"
    assert (project / "new.py").read_text() == "seeded new\n"
    retained = tasks.read_record("apply-child")
    assert retained["applied_at"] and retained["processed_at"]
    backup = json.loads(
        (tasks.state_root() / "apply-child" / "application-backup.json").read_text()
    )
    assert backup["changes"] == child_result["changes"]
    assert child["job_id"] in processed_jobs(tmp_path)


def test_a_failed_continuation_cannot_apply_and_continues_from_its_cumulative_edits(
    task_factory, tmp_path, monkeypatch, capsys
):
    # Guard: only a fresh completed execution with a passing requested check
    # is applicable; a failed child retains its cumulative edits, and a second
    # continuation seeds those cumulative edits into a grandchild.
    capture_project(tmp_path, monkeypatch)
    parent, _ = task_factory("failc-parent", "validation_failed")
    assert tasks.handler(continue_args("failc-parent", id="failc-child")) == 0
    capsys.readouterr()
    child = tasks.read_record("failc-child")
    project = Path(parent["project"])

    child_result = run_child_worker(
        tmp_path,
        monkeypatch,
        child,
        edit=lambda root: (root / "hello.py").write_text("final\n"),
        expect_status="validation_failed",
    )
    assert child_result["validation"]["exit_code"] == 7
    assert [(c["path"], c["after"]) for c in child_result["changes"]] == [
        ("hello.py", encode(b"final\n")),
    ]
    assert tasks.handler(argparse.Namespace(action="apply", id="failc-child")) == 1
    assert "only successful tasks" in capsys.readouterr().err
    assert (project / "hello.py").read_text() == "before\n"
    assert "applied_at" not in tasks.read_record("failc-child")

    assert tasks.handler(continue_args("failc-child", id="failc-grandchild")) == 0
    capsys.readouterr()
    grand = tasks.read_record("failc-grandchild")["request"]
    assert grand["continuation"]["parent_id"] == "failc-child"
    assert grand["seed"] == child_result["changes"]
    assert grand["baseline"] == parent["request"]["baseline"]
    assert grand["continuation"]["parent_result_sha256"] == canonical_digest(child_result)
    # The failed child itself is preserved for evidence.
    assert "applied_at" not in tasks.read_record("failc-child")


@pytest.mark.parametrize("failure_stage", ["admission", "census"])
def test_continue_refuses_a_child_without_a_validated_edit_snapshot(
    task_factory, tmp_path, monkeypatch, capsys, failure_stage
):
    capture_project(tmp_path, monkeypatch)
    parent, parent_result = task_factory("setup-parent", "validation_failed")
    assert tasks.handler(continue_args("setup-parent", id="setup-child")) == 0
    capsys.readouterr()
    child = tasks.read_record("setup-child")
    snapshot = tmp_path / "snapshot"
    shutil.copytree(parent["project"], snapshot)
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "coding-task").write_bytes(
        (tasks.state_root() / child["id"] / "request.json").read_bytes()
    )

    with monkeypatch.context() as worker_patch:
        worker_patch.chdir(snapshot)
        worker_patch.setenv("WEFT_PAYLOAD_DIR", str(payload))
        worker_patch.setattr(
            sys,
            "argv",
            ["task-worker", "--task-id", child["id"], "--request-sha256", child["request_sha256"]],
        )
        if failure_stage == "admission":
            from agent_execution import execution_status

            def failed_probe(**kwargs):
                raise OSError("admission service unavailable")

            worker_patch.setattr(execution_status, "probe_status", failed_probe)
        else:
            (snapshot / "outside.py").write_text("original\n")
            stub_writer(worker_patch, lambda root: (root / "outside.py").write_text("changed\n"))
        with pytest.raises(SystemExit) as exited:
            task_worker.main()
        assert exited.value.code == 1

    result = json.loads((snapshot / tasks.result_path(child["id"])).read_text())
    assert result["worker"] is None
    assert result["changes"] == []
    assert encode((snapshot / "hello.py").read_bytes()) == parent_result["changes"][0]["after"]
    publish_child(tmp_path, child["job_id"], result, status="failed")
    capsys.readouterr()
    runs_before = weft_runs(tmp_path)
    assert tasks.handler(continue_args("setup-child", id="setup-grandchild")) == 1
    assert weft_runs(tmp_path) == runs_before
    assert not (tasks.state_root() / "setup-grandchild" / "task.json").exists()
    assert tasks.read_record("setup-parent") == parent
    assert tasks.retrieve(parent)["changes"] == parent_result["changes"]
