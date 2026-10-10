"""Carried relative symlinks: inventory, confinement, census, and application."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_task_outcomes import task_factory as parent_task_factory

from agent_offload import task_worker, tasks
from agent_offload.task_protocol import (
    MAX_LINK_HOPS,
    SCHEMA,
    TaskError,
    check_symlink_collisions,
    digest,
    encode,
    link_inventory,
    manifest,
    require_confined,
    validate_changes,
    validate_symlinks,
)

task_factory = parent_task_factory

# --- protocol: retained inventory shape --------------------------------------


@pytest.mark.parametrize(
    "links",
    [
        [],
        {"/abs": "x"},
        {"a/../b": "x"},
        {"./a": "x"},
        {"a\\b": "x"},
        {"l": "/absolute"},
        {"l": ""},
        {"l": "a\x00b"},
        {".git/config": "x"},
        {".venv/bin/python": "x"},
        {"a": "x", "A": "y"},
        {"a": "x", "a/b": "y"},
        {"a": "x", "A/child": "y"},
        {"A/child": "y", "a": "x"},
        {"a//b": "x"},
        {"a/./b": "x"},
        {"a/": "x"},
        {None: "x"},
        {"l": 3},
    ],
)
def test_the_retained_symlink_shape_is_strict(links):
    with pytest.raises(TaskError):
        validate_symlinks(links)


def test_an_absent_inventory_defaults_to_empty_but_explicit_null_refuses(tmp_path, monkeypatch):
    request = linked_request(tmp_path)
    stub_writer(monkeypatch)
    del request["symlinks"]
    (tmp_path / "alias.py").unlink()
    with pytest.raises(TaskError):
        task_worker.execute({**request, "symlinks": None}, tmp_path)
    assert task_worker.execute(request, tmp_path)["status"] == "completed"


def test_symlink_collisions_with_snapshot_files_are_refused():
    with pytest.raises(TaskError):
        check_symlink_collisions({"hello.py": "target.py"}, ["hello.py"])
    with pytest.raises(TaskError, match="sits under a snapshot file"):
        check_symlink_collisions({"hello.py/inner": "x"}, ["hello.py"])
    with pytest.raises(TaskError, match="covers snapshot file"):
        check_symlink_collisions({"shared": "real"}, ["shared/keep.txt"])
    # Disjoint inventories pass, including links nested inside a real directory.
    check_symlink_collisions({"nested/launcher": "../real.txt"}, ["real.txt"])


# --- protocol: confinement ---------------------------------------------------


def test_confined_links_keep_literal_targets_through_chains(tmp_path):
    (tmp_path / "real.txt").write_text("x")
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "keep.txt").write_text("y")
    (tmp_path / "first").symlink_to("second")
    (tmp_path / "second").symlink_to("real.txt")
    (tmp_path / "join").symlink_to("shared")
    (tmp_path / "through").symlink_to("join/keep.txt")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "launcher").symlink_to("../real.txt")
    (tmp_path / "nested" / "rootlink").symlink_to("..")
    links = link_inventory(tmp_path)
    # The literal targets are the evidence every later census compares.
    assert links == {
        "first": "second",
        "second": "real.txt",
        "join": "shared",
        "through": "join/keep.txt",
        "nested/launcher": "../real.txt",
        "nested/rootlink": "..",
    }
    assert require_confined(tmp_path, "through", "join/keep.txt") == "shared/keep.txt"
    assert require_confined(tmp_path, "nested/launcher", "../real.txt") == "real.txt"
    # A parent step that lands on the project root itself stays confined.
    assert require_confined(tmp_path, "nested/rootlink", "..") == "."


def test_nested_trailing_separator_requires_a_directory(tmp_path):
    (tmp_path / "file").write_text("ordinary")
    (tmp_path / "directory").mkdir()
    (tmp_path / "inner").symlink_to("file/")
    (tmp_path / "outer").symlink_to("inner")
    with pytest.raises(TaskError):
        require_confined(tmp_path, "outer", "inner")
    (tmp_path / "inner").unlink()
    (tmp_path / "inner").symlink_to("directory/")
    assert require_confined(tmp_path, "outer", "inner") == "directory"


@pytest.mark.parametrize("target", ["file/", "file/.", "missing/../file"])
def test_literal_traversal_does_not_normalize_away_errors(tmp_path, target):
    (tmp_path / "file").write_text("ordinary")
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(TaskError):
        require_confined(tmp_path, "link", target)


def test_finite_repeated_links_are_not_mistaken_for_a_loop(tmp_path):
    (tmp_path / "real").write_text("ok")
    (tmp_path / "again").symlink_to(".")
    (tmp_path / "outer").symlink_to("/".join(["again"] * (MAX_LINK_HOPS - 1) + ["real"]))
    assert require_confined(tmp_path, "outer", (tmp_path / "outer").readlink().as_posix()) == "real"
    target = "/".join(["again"] * MAX_LINK_HOPS + ["real"])
    (tmp_path / "outer").unlink()
    (tmp_path / "outer").symlink_to(target)
    with pytest.raises(TaskError):
        require_confined(tmp_path, "outer", target)


def make_absolute(root: Path) -> None:
    root.joinpath("l").symlink_to(root / "hello.txt")


def make_escape(root: Path) -> None:
    root.joinpath("l").symlink_to("../outside")


def make_indirect_escape(root: Path) -> None:
    root.joinpath("a").symlink_to("b")
    root.joinpath("b").symlink_to("../../outside")


def make_intermediate_escape(root: Path) -> None:
    root.joinpath("a").symlink_to("b")
    root.joinpath("b").symlink_to("../outside")
    root.joinpath("l").symlink_to("a/hello.txt")


def make_cycle(root: Path) -> None:
    root.joinpath("a").symlink_to("b")
    root.joinpath("b").symlink_to("a")


def make_protected_target(root: Path) -> None:
    root.joinpath("l").symlink_to(".git/config")


def make_excluded_tree_target(root: Path) -> None:
    root.joinpath("l").symlink_to(".venv/bin/python")


def make_dangling(root: Path) -> None:
    root.joinpath("l").symlink_to("gone.txt")


def make_too_deep(root: Path) -> None:
    for index in range(MAX_LINK_HOPS):
        root.joinpath(f"l{index}").symlink_to(f"l{index + 1}")
    root.joinpath(f"l{MAX_LINK_HOPS}").symlink_to("hello.txt")


@pytest.mark.parametrize(
    "make,reason",
    [
        (make_absolute, None),
        (make_escape, "escapes the project"),
        (make_indirect_escape, "escapes the project"),
        (make_intermediate_escape, "escapes the project"),
        (make_cycle, None),
        (make_protected_target, "excludes"),
        (make_excluded_tree_target, "excludes"),
        (make_dangling, "does not resolve"),
        (make_too_deep, "too deep"),
    ],
)
def test_unsafe_links_refuse_the_inventory(tmp_path, make, reason):
    (tmp_path / "hello.txt").write_text("x")
    make(tmp_path)
    with pytest.raises(TaskError, match=reason):
        tasks.confined_links(tmp_path, lambda path: False)


def test_a_chain_within_the_hop_bound_is_carried(tmp_path):
    (tmp_path / "hello.txt").write_text("x")
    for index in range(MAX_LINK_HOPS - 1):
        tmp_path.joinpath(f"l{index}").symlink_to(f"l{index + 1}")
    tmp_path.joinpath(f"l{MAX_LINK_HOPS - 1}").symlink_to("hello.txt")
    assert tasks.confined_links(tmp_path, lambda path: False)["l0"] == "l1"


def test_weft_exclusions_govern_the_link_inventory(tmp_path):
    (tmp_path / "harness" / "data.txt").parent.mkdir(parents=True)
    (tmp_path / "harness" / "data.txt").write_text("d")
    (tmp_path / "alias.txt").symlink_to("harness/data.txt")
    (tmp_path / "harness" / "inner").symlink_to("data.txt")

    def excluded(path: Path) -> bool:
        return "harness" in path.parts

    # A link kept by the census but pointing into an excluded tree would be
    # missing from the snapshot, so it refuses instead of failing remotely.
    with pytest.raises(TaskError, match="excluded from the snapshot: alias.txt"):
        tasks.confined_links(tmp_path, excluded)
    # A link inside the excluded tree is dropped with it.
    assert link_inventory(tmp_path, excluded=excluded) == {"alias.txt": "harness/data.txt"}


# --- protocol: changes never address a carried link --------------------------


def linked_changes_request(root: Path) -> dict:
    (root / "hello.py").write_text("print('before')\n")
    (root / "hello.py").chmod(0o644)
    (root / "shared").mkdir()
    (root / "shared" / "keep.txt").write_text("keep\n")
    return {
        "baseline": manifest(root, ["hello.py"]),
        "carried": {},
        "symlinks": {"alias.py": "hello.py", "shared": "real"},
    }


@pytest.mark.parametrize(
    "path",
    ["alias.py", "alias.py/deep", "shared", "shared/nested", "ALIAS.PY/deep", "SHARED/nested"],
)
def test_forged_changes_cannot_address_a_link_or_its_descendants(tmp_path, path):
    request = linked_changes_request(tmp_path)
    change = {
        "path": path,
        "before": None,
        "after": encode(b"injected\n"),
        "before_mode": None,
        "after_mode": 0o644,
    }
    with pytest.raises(TaskError, match="carried symlink"):
        validate_changes(request, [change])


def test_changes_to_the_canonical_target_behind_a_link_are_ordinary(tmp_path):
    request = linked_changes_request(tmp_path)
    changes = [
        {
            "path": "hello.py",
            "before": encode(b"print('before')\n"),
            "after": encode(b"print('after')\n"),
            "before_mode": 0o644,
            "after_mode": 0o644,
        }
    ]
    assert validate_changes(request, changes) == changes


# --- worker: the three inventory censuses ------------------------------------


def linked_request(root: Path, *, links: object = None, **overrides) -> dict:
    (root / "hello.py").write_text("print('before')\n")
    (root / "hello.py").chmod(0o644)
    (root / "alias.py").symlink_to("hello.py")
    request = {
        "schema_version": SCHEMA,
        "id": "linked",
        "brief": "Edit hello.py",
        "selector": "openai-codex/gpt-6-astra",
        "timeout": 30,
        "check": [],
        "baseline": manifest(root, ["hello.py"]),
        "carried": {},
        "symlinks": {"alias.py": "hello.py"} if links is None else links,
        "execution_sha256": "unused-by-simulated-writer",
        "offload_sha256": "unused-by-simulated-writer",
        "request_sha256": "a" * 64,
    }
    request.update(overrides)
    return request


def stub_writer(monkeypatch, edit=None):
    launched = []

    def write(**kwargs):
        launched.append(True)
        if edit is not None:
            edit(Path(kwargs["cwd"]))
        return SimpleNamespace(status="completed", harness=SimpleNamespace(stdout=""), to_dict=dict)

    monkeypatch.setattr(task_worker, "execute_worker", write)
    monkeypatch.setattr(task_worker, "omp_final_text", lambda output: "done")
    return launched


def test_worker_verifies_the_retained_inventory_before_generation(tmp_path, monkeypatch):
    request = linked_request(tmp_path)
    launched = stub_writer(monkeypatch)

    def mutate(root: Path) -> None:
        root.joinpath("extra.lnk").symlink_to("hello.py")

    mutate(tmp_path)
    # An added link refuses before anything reads or writes the snapshot, even
    # a writer that would never have touched it.
    with pytest.raises(TaskError, match="symlink"):
        task_worker.execute(request, tmp_path)
    assert launched == []


@pytest.mark.parametrize(
    "mutation",
    ["delete", "retarget-identical", "replace", "escape"],
)
def test_worker_census_refuses_every_link_drift_kind(tmp_path, monkeypatch, mutation):
    request = linked_request(tmp_path)
    stub_writer(monkeypatch)
    alias = tmp_path / "alias.py"
    if mutation == "delete":
        alias.unlink()
    elif mutation == "retarget-identical":
        # The resolved contents match exactly; the link itself is the evidence.
        (tmp_path / "copy.py").write_text("print('before')\n")
        alias.unlink()
        alias.symlink_to("copy.py")
    elif mutation == "replace":
        alias.unlink()
        alias.write_text("print('before')\n")
    elif mutation == "escape":
        alias.unlink()
        alias.symlink_to("../outside")
    with pytest.raises(TaskError, match="symlink"):
        task_worker.execute(request, tmp_path)


def test_worker_refuses_an_unsafe_link_retained_in_the_request(tmp_path):
    request = linked_request(tmp_path, links={"alias.py": "../outside/hello.py"})
    (tmp_path / "alias.py").unlink()
    (tmp_path / "alias.py").symlink_to("../outside/hello.py")
    # The inventory matches the tree exactly; confinement still refuses it.
    with pytest.raises(TaskError, match="symlink escapes the project"):
        task_worker.execute(request, tmp_path)


def test_worker_refuses_a_malformed_retained_inventory(tmp_path):
    request = linked_request(tmp_path, links=["not", "a", "map"])
    with pytest.raises(TaskError, match="task symlinks"):
        task_worker.execute(request, tmp_path)


def test_worker_refuses_an_inventory_that_collides_with_the_baseline(tmp_path):
    request = linked_request(tmp_path, links={"hello.py": "alias.py"})
    with pytest.raises(TaskError):
        task_worker.execute(request, tmp_path)


def test_worker_refuses_a_writer_that_touches_a_link(tmp_path, monkeypatch):
    request = linked_request(tmp_path)

    def mutate(root: Path) -> None:
        root.joinpath("hello.py").write_text("print('after')\n")
        root.joinpath("alias.py").unlink()

    stub_writer(monkeypatch, mutate)
    # The writer's file edits are discarded with the attempt: a deleted link is
    # a failed task, not a completed one with an undeletable extra.
    with pytest.raises(TaskError, match="snapshot symlinks differ"):
        task_worker.execute(request, tmp_path)


def test_a_check_that_retargets_a_link_fails_validation(tmp_path, monkeypatch):
    import sys

    request = linked_request(
        tmp_path,
        check=[
            sys.executable,
            "-c",
            "import os; os.remove('alias.py'); os.symlink('helper.py', 'alias.py')",
        ],
    )
    (tmp_path / "helper.py").write_text("helper\n")

    def write_proposal(root: Path) -> None:
        root.joinpath("hello.py").write_text("print('after')\n")

    stub_writer(monkeypatch, write_proposal)
    result = task_worker.execute(request, tmp_path)
    # The third census, after validation, makes the check's link mutation a
    # retrievable validation failure that can never be applied.
    assert result["status"] == "validation_failed"
    assert result["validation"]["exit_code"] == 0
    assert [change["path"] for change in result["changes"]] == ["hello.py"]


def test_a_check_that_preserves_the_links_passes(tmp_path, monkeypatch):
    import sys

    request = linked_request(
        tmp_path,
        check=[
            sys.executable,
            "-c",
            "assert __import__('os').readlink('alias.py') == 'hello.py'",
        ],
    )

    def write_proposal(root: Path) -> None:
        root.joinpath("hello.py").write_text("print('after')\n")

    stub_writer(monkeypatch, write_proposal)
    result = task_worker.execute(request, tmp_path)
    assert result["status"] == "completed"


# --- consumers: retained inventory across resume, continuation, apply --------


def retain_links(tmp_path, record, result, links: dict) -> None:
    """Amend a retained request to carry a link inventory, digest-consistent."""
    record["request"]["symlinks"] = links
    payload = tasks.state_root() / record["id"] / "request.json"
    tasks.save(payload, record["request"])
    record["request_sha256"] = digest(payload.read_bytes())
    tasks.save(tasks.state_root() / record["id"] / "task.json", record)
    result["request_sha256"] = record["request_sha256"]
    replies = tmp_path / "replies.json"
    payloads = json.loads(replies.read_text())
    payloads[record["job_id"]]["result"] = result
    replies.write_text(json.dumps(payloads))


def weft_runs(tmp_path) -> list[list[str]]:
    file = tmp_path / "weft-calls"
    if not file.exists():
        return []
    calls = [json.loads(line) for line in file.read_text().splitlines()]
    return [call for call in calls if call[0] == "run"]


def test_apply_verifies_the_retained_inventory_before_writes(task_factory, tmp_path):
    record, result = task_factory("linked-apply")
    project = Path(record["project"])
    (project / "alias.py").symlink_to("hello.py")
    retain_links(tmp_path, record, result, {"alias.py": "hello.py"})
    assert tasks.handler(argparse.Namespace(action="apply", id="linked-apply")) == 0
    assert (project / "hello.py").read_text() == "after\n"
    # The applied edits never touched the carried link or invented a target.
    assert (project / "alias.py").is_symlink()
    assert (project / "alias.py").readlink() == Path("hello.py")


@pytest.mark.parametrize("drift", ["retarget", "delete", "add"])
def test_apply_refuses_link_drift_before_any_write(task_factory, tmp_path, drift):
    record, result = task_factory(f"drift-apply-{drift}")
    project = Path(record["project"])
    (project / "alias.py").symlink_to("hello.py")
    (project / "other.py").write_text("other\n")
    retain_links(tmp_path, record, result, {"alias.py": "hello.py"})
    if drift == "retarget":
        (project / "alias.py").unlink()
        (project / "alias.py").symlink_to("other.py")
    elif drift == "delete":
        (project / "alias.py").unlink()
    elif drift == "add":
        (project / "extra.lnk").symlink_to("hello.py")
    exit_code = tasks.handler(argparse.Namespace(action="apply", id=record["id"]))
    assert exit_code == 1
    # Nothing was written: the proposal is not on disk and no backup exists.
    assert (project / "hello.py").read_text() == "before\n"
    assert not (tasks.state_root() / record["id"] / "application-backup.json").exists()


@pytest.mark.parametrize("action", ["apply", "resume"])
def test_local_guards_ignore_excluded_links_but_refuse_included_drift(
    task_factory, tmp_path, action
):
    record, result = task_factory(f"excluded-{action}")
    project = Path(record["project"])
    (project / "alias.py").symlink_to("hello.py")
    (project / "other.py").write_text("other\n")
    (project / ".cache").mkdir()
    (project / ".cache" / "dangling").symlink_to("../missing")
    (project / "ambient").symlink_to("../missing")
    retain_links(tmp_path, record, result, {"alias.py": "hello.py"})
    if action == "resume":
        with tasks.locked(record["id"]) as directory:
            retained = tasks.read_record(record["id"])
            retained.update(job_id=None, status="submitting", submission_started=False)
            tasks.save(directory / "task.json", retained)
    (project / "alias.py").unlink()
    (project / "alias.py").symlink_to("other.py")
    assert tasks.handler(argparse.Namespace(action=action, id=record["id"])) == 1
    assert weft_runs(tmp_path) == []
    assert (project / "hello.py").read_text() == "before\n"
    (project / "alias.py").unlink()
    (project / "alias.py").symlink_to("hello.py")
    assert tasks.handler(argparse.Namespace(action=action, id=record["id"])) == 0
    assert len(weft_runs(tmp_path)) == (1 if action == "resume" else 0)
    if action == "apply":
        assert (project / "hello.py").read_text() == "after\n"
    assert (project / ".cache" / "dangling").is_symlink()


def test_an_explicit_empty_inventory_refuses_a_new_link(task_factory, tmp_path):
    record, result = task_factory("empty-record")
    project = Path(record["project"])
    retain_links(tmp_path, record, result, {})
    (project / "late.py").symlink_to("hello.py")
    assert tasks.handler(argparse.Namespace(action="apply", id="empty-record")) == 1
    assert (project / "hello.py").read_text() == "before\n"
    assert not (tasks.state_root() / "empty-record" / "application-backup.json").exists()


def test_resume_refuses_a_retargeted_link_before_any_send(task_factory, tmp_path, capsys):
    record, result = task_factory("linked-resume")
    project = Path(record["project"])
    (project / "alias.py").symlink_to("hello.py")
    (project / "other.py").write_text("other\n")
    retain_links(tmp_path, record, result, {"alias.py": "hello.py"})
    with tasks.locked("linked-resume") as directory:
        retained = tasks.read_record("linked-resume")
        retained.update(job_id=None, status="submitting", submission_started=False)
        tasks.save(directory / "task.json", retained)
    (project / "alias.py").unlink()
    (project / "alias.py").symlink_to("other.py")
    assert tasks.handler(argparse.Namespace(action="resume", id="linked-resume")) == 1
    assert weft_runs(tmp_path) == []
    # The unchanged tree still resumes into a queued job.
    (project / "alias.py").unlink()
    (project / "alias.py").symlink_to("hello.py")
    capsys.readouterr()
    assert tasks.handler(argparse.Namespace(action="resume", id="linked-resume")) == 0
    assert len(weft_runs(tmp_path)) == 1
    assert tasks.read_record("linked-resume")["job_id"]


@pytest.mark.parametrize("accepted", [True, False])
def test_initial_ambiguous_receipt_precedes_local_link_drift(task_factory, tmp_path, accepted):
    record, result = task_factory(f"initial-receipt-{'yes' if accepted else 'no'}")
    project = Path(record["project"])
    (project / "alias.py").symlink_to("hello.py")
    (project / "other.py").write_text("other\n")
    retain_links(tmp_path, record, result, {"alias.py": "hello.py"})
    with tasks.locked(record["id"]) as directory:
        retained = tasks.read_record(record["id"])
        retained.update(job_id=None, status="submission_unknown", submission_started=True)
        tasks.save(directory / "task.json", retained)
    if not accepted:
        replies = tmp_path / "replies.json"
        payloads = json.loads(replies.read_text())
        del payloads[record["job_id"]]
        replies.write_text(json.dumps(payloads))
    (project / "alias.py").unlink()
    (project / "alias.py").symlink_to("other.py")
    exit_code = tasks.handler(argparse.Namespace(action="resume", id=record["id"]))
    assert exit_code == (0 if accepted else 1)
    assert weft_runs(tmp_path) == []
    recovered = tasks.read_record(record["id"])
    if accepted:
        assert (recovered["job_id"], recovered["status"]) == (record["job_id"], "queued")
        assert "bin_dir" not in recovered
    else:
        assert recovered == retained
    assert (project / "alias.py").readlink() == Path("other.py")


def test_continuation_carries_and_verifies_the_link_inventory(task_factory, tmp_path, capsys):
    from test_task_continuation import continue_args

    record, result = task_factory("cont-parent", "validation_failed")
    project = Path(record["project"])
    (project / "alias.py").symlink_to("hello.py")
    retain_links(tmp_path, record, result, {"alias.py": "hello.py"})
    assert tasks.handler(continue_args("cont-parent", id="cont-child")) == 0
    child = tasks.read_record("cont-child")
    assert child["request"]["symlinks"] == {"alias.py": "hello.py"}
    capsys.readouterr()

    # A retargeted link is drift a continuation must not inherit silently.
    (project / "alias.py").unlink()
    (project / "alias.py").symlink_to("gone.py")
    assert tasks.handler(continue_args("cont-parent", id="cont-child2")) == 1
    assert not (tasks.state_root() / "cont-child2" / "task.json").exists()


def external_link(project: Path, target: str) -> None:
    outside = project.parent / "outside.txt"
    outside.write_text("external\n")
    (project / "external.txt").symlink_to(
        str(outside) if target == "absolute" else "../outside.txt"
    )


def serve_rg(tmp_path: Path) -> None:
    # Only the external file lister is substituted. snapshot_census, its
    # exclusions and the local drift guards execute against the real tree.
    rg = tmp_path / "bin" / "rg"
    rg.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "for root, dirs, files in os.walk('.'):\n"
        "    dirs[:] = sorted(d for d in dirs if d not in ('.git', '.jj', '.cache', 'ambient') "
        "and not os.path.islink(os.path.join(root, d)))\n"
        "    for name in sorted(files):\n"
        "        path = os.path.join(root, name)\n"
        "        if not os.path.islink(path): print(os.path.relpath(path, '.'))\n"
    )
    rg.chmod(0o755)


@pytest.mark.parametrize("target", ["absolute", "relative"])
def test_legacy_apply_accepts_unchanged_external_link(task_factory, target):
    record, _ = task_factory(f"legacy-apply-{target}")
    project = Path(record["project"])
    external_link(project, target)
    assert "symlinks" not in record["request"]
    assert tasks.handler(argparse.Namespace(action="apply", id=record["id"])) == 0
    assert (project / "hello.py").read_text() == "after\n"
    assert (project / "external.txt").is_symlink()


@pytest.mark.parametrize("path", ["external.txt", "alias/new.py"])
def test_legacy_apply_refuses_alias_destinations_before_writing(task_factory, tmp_path, path):
    record, result = task_factory(f"alias-{path.split('/')[0].replace('.', '-')}")
    project = Path(record["project"])
    if path == "external.txt":
        external_link(project, "absolute")
    else:
        outside = tmp_path / "external-dir"
        outside.mkdir()
        (project / "alias").symlink_to(outside)
    result["changes"].append(
        {
            "path": path,
            "before": None,
            "after": encode(b"wrong\n"),
            "before_mode": None,
            "after_mode": 0o644,
        }
    )
    replies = tmp_path / "replies.json"
    payloads = json.loads(replies.read_text())
    payloads[record["job_id"]]["result"] = result
    replies.write_text(json.dumps(payloads))
    assert tasks.handler(argparse.Namespace(action="apply", id=record["id"])) == 1
    assert (project / "hello.py").read_text() == "before\n"
    assert not (tasks.state_root() / record["id"] / "application-backup.json").exists()
    if path == "external.txt":
        assert (tmp_path / "outside.txt").read_text() == "external\n"
    else:
        assert not (tmp_path / "external-dir" / "new.py").exists()


@pytest.mark.parametrize("target", ["absolute", "relative"])
def test_legacy_initial_resume_preserves_baseline_and_reconciles_first(
    task_factory, tmp_path, target
):
    record, _ = task_factory(f"legacy-resume-{target}")
    project = Path(record["project"])
    external_link(project, target)
    directory = tasks.state_root() / record["id"]
    retained = tasks.read_record(record["id"])
    retained.update(job_id=None, status="submitting", submission_started=False)
    tasks.save(directory / "task.json", retained)
    (project / "hello.py").write_text("drift\n")
    assert tasks.handler(argparse.Namespace(action="resume", id=record["id"])) == 1
    assert weft_runs(tmp_path) == []
    (project / "hello.py").write_text("before\n")
    assert tasks.handler(argparse.Namespace(action="resume", id=record["id"])) == 0
    assert len(weft_runs(tmp_path)) == 1

    # An accepted receipt wins over even a subsequent baseline drift.
    retained = tasks.read_record(record["id"])
    retained.update(job_id=None, status="submission_unknown", submission_started=True)
    tasks.save(directory / "task.json", retained)
    (project / "hello.py").write_text("drift again\n")
    assert tasks.handler(argparse.Namespace(action="resume", id=record["id"])) == 0
    assert tasks.read_record(record["id"])["job_id"] == record["job_id"]
    assert len(weft_runs(tmp_path)) == 1


@pytest.mark.parametrize("target", ["absolute", "relative"])
def test_legacy_continuation_preserves_absence_through_child_guards(task_factory, tmp_path, target):
    from test_task_continuation import continue_args

    parent, _ = task_factory(f"legacy-parent-{target}", "validation_failed")
    project = Path(parent["project"])
    external_link(project, target)
    serve_rg(tmp_path)
    assert tasks.handler(continue_args(parent["id"], id=f"legacy-child-{target}")) == 0
    child = tasks.read_record(f"legacy-child-{target}")
    assert "symlinks" not in child["request"]
    assert "symlinks" not in json.loads(
        (tasks.state_root() / child["id"] / "request.json").read_text()
    )
    tasks.check_baseline_unchanged(child)
    tasks.check_links_unchanged(child)
    # A second failed outcome can be continued without creating a false {}.
    # A separate child result is exercised end-to-end in continuation tests.
    retained = tasks.read_record(child["id"])
    retained.update(job_id=None, status="submitting", submission_started=False)
    tasks.save(tasks.state_root() / child["id"] / "task.json", retained)
    assert tasks.handler(argparse.Namespace(action="resume", id=child["id"])) == 0
    assert len(weft_runs(tmp_path)) == 2


def test_legacy_classifier_accepts_absolute_target_into_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("main\n")
    target = str(project / "main.py")
    (project / "alias").symlink_to(target)

    assert tasks.legacy_links(project, lambda _: False) == {"alias": target}


def test_legacy_classifier_accepts_lexical_escape_that_reenters_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("main\n")
    target = "../project/main.py"
    (project / "alias").symlink_to(target)

    assert tasks.legacy_links(project, lambda _: False) == {"alias": target}


def test_legacy_classifier_refuses_carried_alias_to_external_link(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "outside.txt").write_text("outside\n")
    (project / "outside").symlink_to("../outside.txt")
    (project / "alias").symlink_to("outside")

    with pytest.raises(TaskError):
        tasks.legacy_links(project, lambda _: False)


@pytest.mark.parametrize("target", ["absolute", "relative"])
def test_legacy_resubmission_allows_external_but_not_in_root_links(
    task_factory, tmp_path, monkeypatch, target
):
    record, _ = task_factory(f"legacy-retry-{target}")
    project = Path(record["project"])
    external_link(project, target)
    alias = project / "alias.py"
    alias.symlink_to("hello.py")
    monkeypatch.setattr(
        tasks,
        "ensure_worker",
        lambda retained: retained.update(bin_dir="/worker/bin", build_id="build"),
    )
    with tasks.locked(record["id"]) as directory, pytest.raises(TaskError):
        tasks.resubmit(tasks.read_record(record["id"]), directory)
    assert weft_runs(tmp_path) == []
    alias.unlink()
    with tasks.locked(record["id"]) as directory:
        tasks.resubmit(tasks.read_record(record["id"]), directory)
    assert len(weft_runs(tmp_path)) == 1


def test_legacy_continuation_and_child_resume_refuse_new_ordinary_paths(task_factory, tmp_path):
    from test_task_continuation import continue_args

    parent, _ = task_factory("legacy-paths", "validation_failed")
    project = Path(parent["project"])
    external_link(project, "absolute")
    serve_rg(tmp_path)
    (project / "new.py").write_text("local\n")
    assert tasks.handler(continue_args(parent["id"], id="no-paths")) == 1
    assert not (tasks.state_root() / "no-paths" / "task.json").exists()
    (project / "new.py").unlink()
    assert tasks.handler(continue_args(parent["id"], id="paths-child")) == 0
    child = tasks.read_record("paths-child")
    retained = tasks.read_record(child["id"])
    retained.update(job_id=None, status="submitting", submission_started=False)
    tasks.save(tasks.state_root() / child["id"] / "task.json", retained)
    (project / "new.py").write_text("local\n")
    assert tasks.handler(argparse.Namespace(action="resume", id=child["id"])) == 1
    assert len(weft_runs(tmp_path)) == 1


def test_legacy_continuation_rejects_unrecorded_in_root_link(task_factory, tmp_path):
    from test_task_continuation import continue_args

    parent, _ = task_factory("legacy-inroot", "validation_failed")
    (Path(parent["project"]) / "alias.py").symlink_to("hello.py")
    serve_rg(tmp_path)
    assert tasks.handler(continue_args(parent["id"], id="no-inroot")) == 1
    assert not (tasks.state_root() / "no-inroot" / "task.json").exists()
    assert weft_runs(tmp_path) == []


@pytest.mark.parametrize("target", ["absolute", "relative"])
def test_fresh_submit_still_refuses_external_links(task_factory, tmp_path, target):
    record, _ = task_factory(f"existing-{target}")
    project = Path(record["project"])
    external_link(project, target)
    serve_rg(tmp_path)
    with pytest.raises(TaskError):
        tasks.submit(
            project,
            "Edit hello.py",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=30,
            check=[],
            identity=f"fresh-{target}",
        )
    assert not (tasks.state_root() / f"fresh-{target}" / "task.json").exists()
    assert weft_runs(tmp_path) == []


@pytest.mark.parametrize("inventory", [{}, None], ids=["empty", "null"])
def test_explicit_empty_and_null_inventories_remain_strict(task_factory, tmp_path, inventory):
    from test_task_continuation import continue_args

    parent, result = task_factory("explicit-empty", "validation_failed")
    project = Path(parent["project"])
    external_link(project, "relative")
    serve_rg(tmp_path)
    retain_links(tmp_path, parent, result, inventory)
    assert tasks.handler(continue_args(parent["id"], id="strict-child")) == 1
    assert not (tasks.state_root() / "strict-child" / "task.json").exists()
    retained = tasks.read_record(parent["id"])
    retained.update(job_id=None, status="submitting", submission_started=False)
    tasks.save(tasks.state_root() / parent["id"] / "task.json", retained)
    assert tasks.handler(argparse.Namespace(action="resume", id=parent["id"])) == 1
    assert weft_runs(tmp_path) == []
    applicable, successful = task_factory("strict-apply")
    retain_links(tmp_path, applicable, successful, inventory)
    assert tasks.handler(argparse.Namespace(action="apply", id=applicable["id"])) == 1
    assert (Path(applicable["project"]) / "hello.py").read_text() == "before\n"
