"""Provision the worker build a task runs on, matching the local install exactly.

A task's request pins the source digests of the local agent-offload and
agent-execution packages, and the remote job must run those exact bytes. A
provider returns the absolute ``bin_dir`` of such a build on the task host and
its ``build_id``; the caller then verifies the build's own identity report.

Two providers exist:

- ``ssh`` (the default) copies the two installed packages to a
  content-addressed directory on the host over SSH. It needs only ``python3``
  3.11 or later (or ``uv``) there, plus Bun for the OMP runtime.
- ``agent-host-sync`` delegates to that tool's ``ensure`` command, which builds
  from the checkouts it deploys.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

import agent_execution
from agent_execution.identity import source_sha256

import agent_offload

from .protocol import package_source_sha256
from .task_protocol import TaskError

ENSURE_SCHEMA = "agent-host-sync.ensure/v1"
# Allow agent-host-sync's 45-minute source-lock wait plus time for a cold build.
ENSURE_TIMEOUT = 3600
SSH_TIMEOUT = 900
ENTRY_POINTS = {
    "agent-offload": "cli",
    "agent-offload-worker": "worker",
    "agent-offload-task-worker": "task_worker",
}


@dataclass(frozen=True)
class WorkerBuild:
    build_id: str
    bin_dir: str
    offload_sha256: str
    execution_sha256: str


def ensure_worker_build(
    *, provider: str, host: str, account: str, offload_sha256: str, execution_sha256: str
) -> WorkerBuild:
    if provider == "ssh":
        build = ensure_over_ssh(host, account, offload_sha256, execution_sha256)
    elif provider == "agent-host-sync":
        build = ensure_with_agent_host_sync(host, account)
    else:
        raise TaskError(f"unknown worker provider {provider!r}")
    mismatches = []
    if build.offload_sha256 != offload_sha256:
        mismatches.append("agent-offload")
    if build.execution_sha256 != execution_sha256:
        mismatches.append("agent-execution")
    if mismatches:
        sides = " and ".join(mismatches)
        raise TaskError(
            f"remote {sides} identity differs from the task dispatch: the {provider} provider "
            "built different bytes from the local install. Reinstall agent-offload locally "
            "(`just install` in the agent-offload checkout), or commit or revert the checkout "
            "edits the provider builds from, then resubmit."
        )
    return build


def parse_identity(text: str, *, build_id: str, bin_dir: str) -> WorkerBuild:
    """Read a worker's identity report, which must name the build it was asked for."""
    try:
        identity = json.loads(text)
    except json.JSONDecodeError as error:
        raise TaskError("the worker build printed no identity JSON") from error
    if (
        not isinstance(identity, dict)
        or not isinstance(identity.get("source_sha256"), str)
        or not isinstance(identity.get("execution"), dict)
        or not isinstance(identity["execution"].get("source_sha256"), str)
    ):
        raise TaskError("the worker build printed a malformed identity")
    if identity.get("build_id") != build_id:
        raise TaskError(f"worker build {build_id[:12]} reports build {identity.get('build_id')!r}")
    return WorkerBuild(
        build_id=build_id,
        bin_dir=bin_dir,
        offload_sha256=identity["source_sha256"],
        execution_sha256=identity["execution"]["source_sha256"],
    )


def run_provider(argv: list[str], *, timeout: float, failure: str, stdin: bytes = b"") -> bytes:
    """Run a provider command and return its stdout; any failure names its reason."""
    try:
        result = subprocess.run(
            argv, input=stdin, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TaskError(f"{failure}: {error}") from error
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        detail = detail or result.stdout.decode(errors="replace").strip()
        raise TaskError(f"{failure}: {detail[-2000:] or f'exit {result.returncode}'}")
    return result.stdout


def ssh_target(host: str, account: str) -> str:
    return host if "@" in host else f"{account}@{host}"


def ssh_build_id(offload_sha256: str, execution_sha256: str) -> str:
    material = f"agent-offload.ssh-build/v1\0{offload_sha256}\0{execution_sha256}"
    return hashlib.sha256(material.encode()).hexdigest()


def package_archive() -> bytes:
    """The installed agent_offload and agent_execution packages as a tar archive.

    Copying the installed packages, rather than building from a checkout, makes
    the remote digests equal the local ones by construction. Both packages are
    pure Python with no dependencies; Bun's node_modules are provisioned on the
    host instead of copied.
    """
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for package in (agent_offload, agent_execution):
            root = Path(package.__file__).resolve().parent
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root)
                if path.is_dir() or any(
                    part in ("__pycache__", "node_modules") or part.startswith(".")
                    for part in relative.parts
                ):
                    continue
                archive.add(path, arcname=f"{root.name}/{relative.as_posix()}", recursive=False)
    return stream.getvalue()


def remote_install_script(build_id: str, offload_sha256: str, execution_sha256: str) -> str:
    """Shell run on the host: reuse a finished build, or unpack stdin into a new one.

    A build is published by renaming a complete staging directory, so a reader
    never sees a half-built one and two concurrent installs cannot corrupt it.
    The staged packages are checked against the requested digests first, so
    no failure can leave other bytes published under this build's name.
    """
    if not all(
        re.fullmatch(r"[0-9a-f]{64}", d) for d in (build_id, offload_sha256, execution_sha256)
    ):
        raise ValueError("build and source digests must be lowercase SHA-256 hex")
    shims = "\n".join(
        f"""cat > "$stage/bin/{name}" <<EOF
#!/bin/sh
exec "$python" -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); sys.argv[0] = "{name}"; from agent_offload.entry import {function}; {function}()' "$root/lib" "\\$@"
EOF
chmod +x "$stage/bin/{name}\""""
        for name, function in ENTRY_POINTS.items()
    )
    return f"""set -eu
export PATH="$HOME/.local/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
builds="$HOME/.local/share/agent-offload/builds"
root="$builds/{build_id}"
if [ ! -f "$root/BUILD_ID" ]; then
    python=""
    if command -v uv >/dev/null 2>&1; then
        python="$(uv python find '>=3.11' 2>/dev/null || true)"
    fi
    if [ -z "$python" ] && command -v python3 >/dev/null 2>&1 \\
        && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        python="$(command -v python3)"
    fi
    if [ -z "$python" ]; then
        echo "agent-offload: the host needs python3 3.11 or later, or uv" >&2
        exit 1
    fi
    mkdir -p "$builds"
    stage="$(mktemp -d "$builds/.stage-XXXXXX")"
    trap 'rm -rf "$stage"' EXIT
    mkdir -p "$stage/lib" "$stage/bin"
    tar -xzf - -C "$stage/lib"
{shims}
    "$python" -c 'import sys
from pathlib import Path
lib = Path(sys.argv[1])
sys.path.insert(0, str(lib))
from agent_execution.identity import source_sha256 as execution_sha256
from agent_offload.protocol import source_sha256 as offload_sha256
staged = (offload_sha256(lib / "agent_offload"), execution_sha256(lib / "agent_execution"))
if staged != (sys.argv[2], sys.argv[3]):
    sys.exit("agent-offload: the copied packages do not have the requested digests")' \
        "$stage/lib" {offload_sha256} {execution_sha256}
    "$python" -c 'import sys; sys.path.insert(0, sys.argv[1]); from agent_execution.omp_install import install_omp_runtime; install_omp_runtime()' "$stage/lib"
    echo {build_id} > "$stage/BUILD_ID"
    # rename(2) refuses a nonempty target, so a concurrent install's finished
    # build wins and this one is discarded; `mv` would nest it instead.
    "$python" -c 'import errno, os, sys
try:
    os.rename(sys.argv[1], sys.argv[2])
except OSError as error:
    if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
        raise' "$stage" "$root"
fi
echo "$root/bin"
"$root/bin/agent-offload-worker" identity
"""


def ensure_over_ssh(
    host: str, account: str, offload_sha256: str, execution_sha256: str
) -> WorkerBuild:
    changed = [
        name
        for name, installed, requested in (
            ("agent-offload", package_source_sha256(), offload_sha256),
            ("agent-execution", source_sha256(), execution_sha256),
        )
        if installed != requested
    ]
    if changed:
        raise TaskError(
            f"the installed {' and '.join(changed)} changed since this task was recorded, "
            "so the worker it pinned cannot be built from this install; submit a new task "
            "for the same brief"
        )
    build_id = ssh_build_id(offload_sha256, execution_sha256)
    target = ssh_target(host, account)
    script = remote_install_script(build_id, offload_sha256, execution_sha256)
    stdout = run_provider(
        ["ssh", "-o", "BatchMode=yes", target, "sh -c " + shlex.quote(script)],
        stdin=package_archive(),
        timeout=SSH_TIMEOUT,
        failure=f"worker provisioning on {target} failed",
    ).decode(errors="replace")
    bin_dir, _, identity = stdout.partition("\n")
    if not bin_dir.startswith("/"):
        raise TaskError(f"worker provisioning on {target} printed no build directory")
    return parse_identity(identity, build_id=build_id, bin_dir=bin_dir)


def ensure_with_agent_host_sync(host: str, account: str) -> WorkerBuild:
    argv = [
        "agent-host-sync",
        "--host",
        host,
        "--account",
        account,
        "ensure",
        "--tool",
        "agent-offload",
        "--json",
    ]
    stdout = run_provider(argv, timeout=ENSURE_TIMEOUT, failure="agent-host-sync ensure failed")
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise TaskError("agent-host-sync ensure returned invalid JSON") from error
    if not isinstance(value, dict) or value.get("schema") != ENSURE_SCHEMA:
        raise TaskError(f"agent-host-sync ensure did not return {ENSURE_SCHEMA}")
    builds = value.get("builds")
    peers = (
        [item for item in builds if isinstance(item, dict) and item.get("tool") == "agent-offload"]
        if isinstance(builds, list)
        else []
    )
    if len(peers) != 1:
        raise TaskError("agent-host-sync ensure must return exactly one agent-offload build")
    peer = peers[0]
    identity = peer.get("identity")
    build_id, bin_dir = peer.get("build_id"), peer.get("bin_dir")
    if (
        not isinstance(identity, dict)
        or not isinstance(identity.get("source_sha256"), str)
        or not isinstance(identity.get("execution"), dict)
        or not isinstance(identity["execution"].get("source_sha256"), str)
        or not isinstance(build_id, str)
        or not re.fullmatch(r"[0-9a-f]{64}", build_id)
        or not isinstance(bin_dir, str)
        or not bin_dir.startswith("/")
    ):
        raise TaskError("agent-host-sync ensure returned a malformed agent-offload build")
    return WorkerBuild(
        build_id=build_id,
        bin_dir=bin_dir,
        offload_sha256=identity["source_sha256"],
        execution_sha256=identity["execution"]["source_sha256"],
    )
