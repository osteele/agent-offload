"""Empty project submission and census failure boundaries."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from agent_offload.task_protocol import TaskError
from agent_offload.tasks import read_record, snapshot_names, submit

REAL_RG = shutil.which("rg")


def stub_dir(tmp_path: Path) -> Path:
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    return tools


def empty_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


def fake_rg(
    tmp_path: Path, monkeypatch, *, exit_code: int, stdout: str = "", stderr: str = ""
) -> None:
    """Install an rg that reproduces one documented result: census, diagnostic, status."""
    listing = tmp_path / "rg-stdout"
    listing.write_text(stdout)
    diagnostic = tmp_path / "rg-stderr"
    diagnostic.write_text(stderr)
    script = stub_dir(tmp_path) / "rg"
    script.write_text(
        f'#!/bin/sh\n/bin/cat "{listing}"\n/bin/cat "{diagnostic}" >&2\nexit {exit_code}\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(stub_dir(tmp_path)))


def fake_weft(
    tmp_path: Path, monkeypatch, *, excludes: list[str], receipt: dict | None = None
) -> Path:
    """Install a weft answering `sync inspect`, optionally accepting a queued `run`.

    Every invocation is logged; any weft command without a scripted answer
    fails loudly, as an unexpected one should.
    """
    inspection = tmp_path / "inspection.json"
    inspection.write_text(json.dumps({"local_dir": "unused", "excludes": excludes}))
    calls = tmp_path / "weft-calls"
    calls.write_text("")
    accepted = ""
    if receipt is not None:
        answer = tmp_path / "receipt.json"
        answer.write_text(json.dumps(receipt))
        accepted = f'[ "$1" = "run" ] && /bin/cat "{answer}" && exit 0\n'
    script = stub_dir(tmp_path) / "weft"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{calls}"\n'
        f'if [ "$1 $2" = "sync inspect" ]; then /bin/cat "{inspection}"; exit 0; fi\n'
        f"{accepted}"
        "echo 'weft: unexpected command' >&2\nexit 3\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(stub_dir(tmp_path)))
    return calls


def use_real_rg(tools: Path, monkeypatch) -> None:
    """Keep the stub weft while letting `rg` resolve to the installed binary."""
    monkeypatch.setenv("PATH", os.pathsep.join([str(tools), str(Path(REAL_RG).parent)]))


@pytest.mark.skipif(REAL_RG is None, reason="rg is not installed")
def test_real_rg_census_accepts_an_empty_project(tmp_path, monkeypatch):
    project = empty_project(tmp_path)
    tools = stub_dir(tmp_path)
    fake_weft(tmp_path, monkeypatch, excludes=[])
    use_real_rg(tools, monkeypatch)
    assert snapshot_names(project) == []


@pytest.mark.skipif(REAL_RG is None, reason="rg is not installed")
def test_real_rg_census_keeps_hidden_files_and_drops_metadata(tmp_path, monkeypatch):
    project = empty_project(tmp_path)
    (project / "src").mkdir()
    for name, text in (
        ("hello.py", "hello"),
        ("src/module.py", "module"),
        (".hidden", "kept"),
        (".git/config", "[core]"),
        (".jj/store", "store"),
        ("debug.log", "dropped"),
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    tools = stub_dir(tmp_path)
    fake_weft(tmp_path, monkeypatch, excludes=["*.log"])
    use_real_rg(tools, monkeypatch)
    assert sorted(snapshot_names(project)) == [".hidden", "hello.py", "src/module.py"]


def test_a_real_census_failure_is_explicit(tmp_path, monkeypatch):
    project = empty_project(tmp_path)
    fake_rg(tmp_path, monkeypatch, exit_code=2, stderr="rg: unrecognized flag\n")
    fake_weft(tmp_path, monkeypatch, excludes=[])
    with pytest.raises(TaskError, match="unrecognized flag"):
        snapshot_names(project)


def test_a_missing_rg_is_explicit(tmp_path, monkeypatch):
    project = empty_project(tmp_path)
    fake_weft(tmp_path, monkeypatch, excludes=[])
    with pytest.raises(TaskError, match="could not execute rg"):
        snapshot_names(project)


def test_a_failed_inspection_is_still_explicit(tmp_path, monkeypatch):
    # The no-match tolerance belongs to the rg census alone; the other census
    # leg still refuses its own unexpected exit, diagnostics or not.
    project = empty_project(tmp_path)
    fake_rg(tmp_path, monkeypatch, exit_code=1)
    weft = stub_dir(tmp_path) / "weft"
    weft.write_text("#!/bin/sh\nexit 1\n")
    weft.chmod(0o755)
    monkeypatch.setenv("PATH", str(stub_dir(tmp_path)))
    with pytest.raises(TaskError, match="exit 1"):
        snapshot_names(project)


def queued_receipt(identity: str) -> dict:
    return {
        "api_version": "weft.run.receipt.v1",
        "job_id": "wj42",
        "placement_decision": "queued",
        "selected_host": "studio",
        "source_pin": "pin",
        "accepted_immediately": False,
        "deduplicated": False,
        "idempotency_key": identity,
    }


def test_an_empty_project_submits_once_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OFFLOAD_STATE", str(tmp_path / "state"))
    project = empty_project(tmp_path)
    identity = "empty-start"
    calls = fake_weft(tmp_path, monkeypatch, excludes=[], receipt=queued_receipt(identity))
    fake_rg(tmp_path, monkeypatch, exit_code=1)

    def start() -> dict:
        return submit(
            project,
            "Start from nothing",
            host="studio",
            selector="openai-codex/gpt-6-astra",
            timeout=60,
            check=[],
            identity=identity,
        )

    assert start()["status"] == "queued"
    record = read_record(identity)
    assert (record["status"], record["job_id"]) == ("queued", "wj42")
    assert record["request"]["baseline"] == {}
    assert record["request"]["carried"] == {}
    # The identity admits exactly one submission: the retained record refuses a
    # second one without another census or another Weft dispatch.
    with pytest.raises(TaskError, match="already exists"):
        start()
    assert len(calls.read_text().splitlines()) == 2
