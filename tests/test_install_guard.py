import subprocess
import sys

import pytest

# Simulates an install that omitted `--with ../agent-execution`: the finder
# refuses agent_execution before anything imports it.
BLOCK_EXECUTION = """
import importlib.abc
import sys


class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "agent_execution" or name.startswith("agent_execution."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


sys.meta_path.insert(0, Block())
"""


def run_without_execution(statement: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", BLOCK_EXECUTION + statement],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("entry_point", ["cli", "worker", "task_worker"])
def test_entry_point_names_missing_agent_execution_instead_of_a_traceback(
    entry_point: str,
) -> None:
    completed = run_without_execution(
        f"from agent_offload.entry import {entry_point}\n{entry_point}()"
    )

    assert completed.returncode == 1
    assert "agent_execution package is not installed" in completed.stderr
    assert "--with ../agent-execution" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_relay_imports_without_agent_execution() -> None:
    # agent-host-sync's installed-health check imports this from source with
    # only `src` on the path.
    completed = run_without_execution("from agent_offload.relay import relay_source_sha256")

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("entry_point", ["cli", "worker", "task_worker"])
def test_entry_point_refuses_a_non_posix_platform_by_name(entry_point: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import os\nos.name = 'nt'\nfrom agent_offload.entry import {entry_point}\n{entry_point}()",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "runs on macOS and Linux" in completed.stderr
    assert "Traceback" not in completed.stderr
