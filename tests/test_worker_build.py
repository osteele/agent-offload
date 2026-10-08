from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import agent_execution
import pytest
from agent_execution.identity import source_sha256

import agent_offload
from agent_offload import tasks, worker_build
from agent_offload.config import load_config, require_relay_host, require_task_host, task_account
from agent_offload.protocol import package_source_sha256
from agent_offload.protocol import source_sha256 as offload_source_sha256
from agent_offload.task_protocol import TaskError

posix_only = pytest.mark.skipif(os.name != "posix", reason="the worker host runs a POSIX shell")


def test_missing_config_means_no_host_and_the_ssh_provider(isolated_config):
    config = load_config()
    assert (config.task_host, config.relay_host, config.worker_provider) == (None, None, "ssh")
    with pytest.raises(TaskError, match="pass --host"):
        require_task_host(None, config)
    with pytest.raises(TaskError, match=r"\[relay\] host"):
        require_relay_host(None, config)


def test_flags_override_config_and_account_defaults_to_the_local_user(isolated_config):
    isolated_config.write_text(
        '[tasks]\nhost = "box"\n[relay]\nhost = "me@box"\n[worker]\nprovider = "agent-host-sync"\n'
    )
    config = load_config()
    assert require_task_host(None, config) == "box"
    assert require_task_host("other", config) == "other"
    assert require_relay_host(None, config) == "me@box"
    assert config.worker_provider == "agent-host-sync"
    assert task_account(None, config) == __import__("getpass").getuser()
    isolated_config.write_text('[tasks]\naccount = "agent"\n')
    assert task_account(None, load_config()) == "agent"
    assert task_account("ci", load_config()) == "ci"


@pytest.mark.parametrize(
    "text,message",
    [
        ('[task]\nhost = "x"\n', "unknown section"),
        ('[tasks]\nhots = "x"\n', "unknown key tasks.hots"),
        ("[tasks]\nhost = 3\n", "nonempty string"),
        ('[worker]\nprovider = "rsync"\n', "worker.provider must be one of"),
        ("[tasks\n", "config"),
    ],
)
def test_config_errors_are_loud(isolated_config, text, message):
    isolated_config.write_text(text)
    with pytest.raises(TaskError, match=message):
        load_config()


def test_submit_without_a_host_refuses_before_any_work(isolated_config, tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    parser = __import__("argparse").ArgumentParser()
    tasks.build_task_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["task", "submit", str(project), "--prompt", "x"])
    assert tasks.handler(args) == 1
    assert "no host" in capsys.readouterr().err


def test_package_archive_carries_exactly_the_digested_sources(tmp_path):
    with tarfile.open(fileobj=io.BytesIO(worker_build.package_archive())) as archive:
        archive.extractall(tmp_path, filter="data")
    assert offload_source_sha256(tmp_path / "agent_offload") == package_source_sha256()
    assert source_sha256(tmp_path / "agent_execution") == source_sha256()
    assert not list(tmp_path.rglob("__pycache__"))
    assert not list(tmp_path.rglob("node_modules"))


def test_build_id_depends_on_both_digests():
    a, b = "a" * 64, "b" * 64
    assert len({worker_build.ssh_build_id(a, b), worker_build.ssh_build_id(b, a)}) == 2
    assert worker_build.ssh_target("box", "agent") == "agent@box"
    assert worker_build.ssh_target("me@box", "agent") == "me@box"


def fake_host(tmp_path: Path) -> dict[str, str]:
    """A local stand-in for the task host: `ssh` runs the remote command here."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    # ssh -o BatchMode=yes TARGET COMMAND
    ssh.write_text('#!/bin/sh\nshift 3\nexec sh -c "$1"\n')
    # The OMP runtime install runs `bun install` and an import check; succeed
    # without the network. It goes in ~/.local/bin, which the remote script puts
    # first on PATH, so a real Bun elsewhere is never reached.
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    bun = home / ".local/bin/bun"
    bun.write_text("#!/bin/sh\nexit 0\n")
    for path in (ssh, bun):
        path.chmod(0o755)
    python = Path(sys.executable).parent
    return {
        "HOME": str(home),
        "PATH": os.pathsep.join([str(bin_dir), str(python), "/usr/bin", "/bin"]),
    }


@posix_only
def test_ssh_provider_installs_a_build_that_reports_the_local_digests(tmp_path, monkeypatch):
    for name, value in fake_host(tmp_path).items():
        monkeypatch.setenv(name, value)
    offload, execution = package_source_sha256(), source_sha256()
    build = worker_build.ensure_worker_build(
        provider="ssh",
        host="box",
        account="agent",
        offload_sha256=offload,
        execution_sha256=execution,
    )
    root = Path(build.bin_dir).parent
    assert root == tmp_path / "home/.local/share/agent-offload/builds" / build.build_id
    assert (root / "BUILD_ID").read_text().strip() == build.build_id
    assert (build.offload_sha256, build.execution_sha256) == (offload, execution)
    task_worker = root / "bin/agent-offload-task-worker"
    assert os.access(task_worker, os.X_OK)
    assert subprocess.run([task_worker, "--help"], capture_output=True, check=False).returncode == 0
    # A second call reuses the published build rather than replacing it.
    marker = root / "lib/marker"
    marker.write_text("kept")
    again = worker_build.ensure_worker_build(
        provider="ssh",
        host="box",
        account="agent",
        offload_sha256=offload,
        execution_sha256=execution,
    )
    assert again == build and marker.read_text() == "kept"
    assert not list(root.parent.glob(".stage-*"))


@posix_only
@pytest.mark.parametrize("side", ["agent-offload", "agent-execution"])
def test_ssh_provider_refuses_a_pin_the_install_cannot_satisfy_before_copying(
    tmp_path, monkeypatch, side
):
    # A task recorded by an earlier install: copying this install would put
    # other bytes under the pinned build's name.
    for name, value in fake_host(tmp_path).items():
        monkeypatch.setenv(name, value)
    offload, execution = package_source_sha256(), source_sha256()
    if side == "agent-offload":
        offload = "0" * 64
    else:
        execution = "0" * 64
    with pytest.raises(TaskError, match=f"installed {side} changed"):
        worker_build.ensure_worker_build(
            provider="ssh",
            host="box",
            account="agent",
            offload_sha256=offload,
            execution_sha256=execution,
        )
    assert not (tmp_path / "home/.local/share/agent-offload").exists()


@posix_only
def test_host_publishes_nothing_when_the_copied_packages_differ_from_the_pin(tmp_path):
    environment = {**os.environ, **fake_host(tmp_path)}
    offload, execution = "0" * 64, source_sha256()
    build_id = worker_build.ssh_build_id(offload, execution)
    script = worker_build.remote_install_script(build_id, offload, execution)
    completed = subprocess.run(
        ["sh", "-c", script],
        input=worker_build.package_archive(),
        env=environment,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert b"requested digests" in completed.stderr
    builds = tmp_path / "home/.local/share/agent-offload/builds"
    assert list(builds.iterdir()) == []


def test_install_script_refuses_digests_that_are_not_hex():
    with pytest.raises(ValueError):
        worker_build.remote_install_script("a" * 64, "$(reboot)", "b" * 64)


def test_ssh_provider_reports_the_host_failure(monkeypatch):
    def run_provider(argv, **kwargs):
        raise TaskError(f"{kwargs['failure']}: python3 3.11 or later, or uv")

    monkeypatch.setattr(worker_build, "run_provider", run_provider)
    with pytest.raises(TaskError, match="provisioning on agent@box failed: python3"):
        worker_build.ensure_worker_build(
            provider="ssh",
            host="box",
            account="agent",
            offload_sha256=package_source_sha256(),
            execution_sha256=source_sha256(),
        )


def test_identity_must_name_the_requested_build():
    identity = json.dumps(
        {"build_id": "c" * 64, "source_sha256": "a", "execution": {"source_sha256": "b"}}
    )
    with pytest.raises(TaskError, match="reports build"):
        worker_build.parse_identity(identity, build_id="d" * 64, bin_dir="/x/bin")
    build = worker_build.parse_identity(identity, build_id="c" * 64, bin_dir="/x/bin")
    assert (build.offload_sha256, build.execution_sha256) == ("a", "b")


@posix_only
def test_ssh_provider_copies_the_running_install_not_its_checkout(tmp_path):
    """An install built from other bytes than its checkout still gets a matching build.

    Building the host copy from a checkout refused every task once the checkout
    was edited after the install; copying the running packages cannot.
    """
    installed = tmp_path / "installed"
    for package in (agent_offload, agent_execution):
        root = Path(package.__file__).resolve().parent
        shutil.copytree(
            root,
            installed / root.name,
            ignore=shutil.ignore_patterns("__pycache__", "node_modules", ".*"),
        )
    protocol = installed / "agent_offload" / "protocol.py"
    protocol.write_text(protocol.read_text() + "\n# an installed release\n")
    environment = {**os.environ, **fake_host(tmp_path), "PYTHONPATH": str(installed)}
    program = """
import json
from agent_execution.identity import source_sha256
from agent_offload.protocol import package_source_sha256
from agent_offload.worker_build import ensure_worker_build

build = ensure_worker_build(
    provider="ssh",
    host="box",
    account="agent",
    offload_sha256=package_source_sha256(),
    execution_sha256=source_sha256(),
)
print(json.dumps([package_source_sha256(), build.offload_sha256]))
"""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    running, built = json.loads(completed.stdout)
    assert running == built
    assert running != package_source_sha256()
