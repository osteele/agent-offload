"""Durable coding-task contracts and source-change validation."""

from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

SCHEMA = "agent-offload.task/v1"
RESULT_SCHEMA = "agent-offload.task-result/v1"
LIST_SCHEMA = "agent-offload.tasks/v1"
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
EXCLUDED = {
    ".git",
    ".jj",
    ".agent-execution",
    ".venv",
    "node_modules",
    "__pycache__",
    ".build",
    ".ruff_cache",
    ".pytest_cache",
    ".omp",
    ".codex",
    ".claude",
    ".agents",
}


class TaskError(ValueError):
    """A task cannot proceed with the available evidence."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def task_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", value):
        raise TaskError("invalid task identity")
    return value


def result_path(identity: str) -> str:
    return f".agent-execution/offload/{task_id(identity)}.json"


def safe_path(root: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    if not parts or name != PurePosixPath(name).as_posix() or name.startswith("/"):
        raise TaskError(f"invalid relative path: {name}")
    if any(p in {".", ".."} or p.lower() in EXCLUDED for p in parts):
        raise TaskError(f"protected or escaping path: {name}")
    if any(c in name for c in "\x00\n\r\\:"):
        raise TaskError(f"invalid path characters: {name}")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise TaskError(f"symlink is not a task file: {name}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise TaskError(f"path escapes project: {name}")
    return path


def file_bytes(path: Path) -> bytes:
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_FILE:
        raise TaskError(f"task file must be regular and at most {MAX_FILE} bytes: {path}")
    return path.read_bytes()


def manifest(root: Path, names: list[str]) -> dict[str, dict[str, object]]:
    result = {}
    total = 0
    for name in sorted(set(names)):
        path = safe_path(root, name)
        data = file_bytes(path)
        total += len(data)
        if total > MAX_TOTAL or len(result) >= 10000:
            raise TaskError("task snapshot exceeds 64 MiB or 10000 files")
        result[name] = {"sha256": digest(data), "mode": stat.S_IMODE(path.stat().st_mode)}
    return result


def walk_files(root: Path) -> list[str]:
    names = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d.lower() not in EXCLUDED]
        for d in dirs:
            if (Path(directory) / d).is_symlink():
                raise TaskError(f"symlink directory in task output: {d}")
        for name in files:
            names.append((Path(directory) / name).relative_to(root).as_posix())
    return names


def encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_FILE * 2:
        raise TaskError("invalid change content")
    try:
        data = base64.b64decode(value, validate=True)
        data.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise TaskError("changes must contain valid base64 UTF-8 text") from error
    if len(data) > MAX_FILE:
        raise TaskError("change exceeds file size limit")
    return data


def object_json(text: str, schema: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise TaskError("invalid task JSON") from error
    if not isinstance(value, dict) or value.get("schema_version") != schema:
        raise TaskError(f"expected {schema}")
    return value


def validate_changes(request: dict, changes: object) -> list[dict]:
    if not isinstance(changes, list) or len(changes) > 10000:
        raise TaskError("invalid task change list")
    seen: set[str] = set()
    total = 0
    for change in changes:
        if not isinstance(change, dict) or not isinstance(change.get("path"), str):
            raise TaskError("invalid change record")
        name = change["path"]
        safe_path(Path("/task-validation"), name)
        folded = name.casefold()
        if folded in seen:
            raise TaskError("duplicate change path")
        if any(folded.startswith(other + "/") or other.startswith(folded + "/") for other in seen):
            raise TaskError("change paths overlap as a file and directory")
        seen.add(folded)
        baseline = request["baseline"].get(name)
        before = decode(change["before"]) if change.get("before") is not None else None
        after = decode(change["after"]) if change.get("after") is not None else None
        if before is None and after is None:
            raise TaskError("empty change")
        if (baseline is None) != (before is None):
            raise TaskError("change disagrees with baseline existence")
        if baseline and (
            digest(before) != baseline["sha256"] or change["before_mode"] != baseline["mode"]
        ):
            raise TaskError("change disagrees with baseline bytes or permissions")
        if after is not None and change.get("after_mode") not in {0o600, 0o644, 0o700, 0o755}:
            raise TaskError("unsupported output permissions")
        total += len(before or b"") + len(after or b"")
        if total > MAX_TOTAL:
            raise TaskError("change bundle exceeds size limit")
    return changes


def render_diff(changes: list[dict]) -> str:
    pieces = []
    for change in changes:
        name = change["path"]
        before = decode(change["before"]) if change["before"] is not None else b""
        after = decode(change["after"]) if change["after"] is not None else b""
        pieces.extend(
            difflib.unified_diff(
                before.decode().splitlines(True),
                after.decode().splitlines(True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
            )
        )
        if change["before_mode"] != change["after_mode"]:
            pieces.append(f"mode {name}: {change['before_mode']} -> {change['after_mode']}\n")
    return "".join(pieces)
