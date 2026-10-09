"""Provision the worker build a task runs on, matching the local install exactly.

A task's request pins the source digests of the local agent-offload and
agent-execution packages, and the remote job must run those exact bytes. A
provider returns the absolute ``bin_dir`` of such a build on the task host and
its ``build_id``; the caller then verifies the build's own identity report.

There is exactly one mechanism: copy the two installed packages to a
content-addressed directory on the host over SSH. It needs only ``python3``
3.11 or later (or ``uv``) there, plus Bun for the OMP runtime. Because the
copied bytes are the installed ones, the build matches the dispatch by
construction, and edits to any source checkout cannot change what a task
runs.

The configuration still accepts ``[worker] provider = "agent-host-sync"`` so
deployed configuration files keep loading, but it no longer selects a builder:
every task provisions through the installed-package mechanism. The retired
delegation to agent-host-sync's ``ensure`` built from the checkouts that tool
deploys, so an unrelated checkout edit changed (or broke) what tasks ran, and
its digest-mismatch failure could only suggest reinstalling or rewriting the
checkout. That path is gone; see ``ensure_over_ssh`` for the identity guards
that replaced it.
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

from .config import WORKER_PROVIDERS
from .protocol import package_source_sha256
from .task_protocol import TaskError


class PinnedBuildUnavailable(TaskError):
    """The installed packages cannot build a task's pinned worker.

    Unlike a transport or host failure, resuming the task cannot help: the
    pinned bytes no longer exist in any install this machine can copy, and
    they are never reconstructed from a checkout. The task keeps its identity
    and its refusal; a new task pins the current install.
    """


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
    """Return the immutable build for this dispatch, provisioning it if needed.

    ``provider`` is still validated so a mistyped configuration fails loudly,
    and it is accepted for every value the configuration has ever admitted,
    but it no longer selects a builder: a task build is content-addressed
    from the installed, pinned package pair, so provisioning is deterministic
    and independent of any source checkout's state.
    """
    if provider not in WORKER_PROVIDERS:
        raise TaskError(f"unknown worker provider {provider!r}")
    build = ensure_over_ssh(host, account, offload_sha256, execution_sha256)
    if build.offload_sha256 != offload_sha256:
        raise TaskError(
            "worker build agent-offload digest differs from the dispatch "
            f"(expected {offload_sha256}, got {build.offload_sha256})"
        )
    if build.execution_sha256 != execution_sha256:
        raise TaskError(
            "worker build agent-execution digest differs from the dispatch "
            f"(expected {execution_sha256}, got {build.execution_sha256})"
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
    """Copy the installed pair into the pinned content-addressed build.

    The digests are checked before anything is copied: a task recorded by an
    earlier install would otherwise put other bytes under its pinned build's
    name. The host repeats the check on the staged packages before publishing,
    so no failure leaves a wrong build under this build ID.
    """
    changed = [
        (name, installed, requested)
        for name, installed, requested in (
            ("agent-offload", package_source_sha256(), offload_sha256),
            ("agent-execution", source_sha256(), execution_sha256),
        )
        if installed != requested
    ]
    if changed:
        sides = " and ".join(name for name, _, _ in changed)
        detail = "; ".join(
            f"{name} task pinned {requested[:12]}, installed {installed[:12]}"
            for name, installed, requested in changed
        )
        raise PinnedBuildUnavailable(
            f"the installed {sides} changed since this task was recorded ({detail}), so the "
            "worker it pinned cannot be built from this install; the pinned bytes cannot be "
            "reconstructed, and this task identity cannot resume: submit a new task for the "
            "same brief"
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
