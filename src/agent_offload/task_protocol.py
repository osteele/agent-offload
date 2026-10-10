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
import tempfile
from pathlib import Path, PurePosixPath

SCHEMA = "agent-offload.task/v1"
RESULT_SCHEMA = "agent-offload.task-result/v1"
LIST_SCHEMA = "agent-offload.tasks/v1"
SELECTORS_SCHEMA = "agent-offload.task-selectors/v1"
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
# Bound on the links followed while resolving one carried link's target.
MAX_LINK_HOPS = 32
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


def canonical_digest(value: object) -> str:
    """SHA-256 of a JSON value's canonical form: sorted keys, compact separators.

    Large evidence is retained by this digest instead of duplicating its
    bytes; a field carrying one names exactly what is hashed.
    """
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


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


def split_carried(root: Path, names: list[str]) -> tuple[list[str], list[str]]:
    """Separate editable snapshot files from those over the task file limit.

    A project is not disqualified by a large file it does not edit. The
    protocol moves whole file contents, so a file over the limit cannot be an
    edit, but the snapshot still carries it and a check can still read it.
    """
    editable, carried = [], []
    for name in sorted(set(names)):
        info = safe_path(root, name).stat()
        oversized = stat.S_ISREG(info.st_mode) and info.st_size > MAX_FILE
        (carried if oversized else editable).append(name)
    return editable, carried


def census(root: Path, names: list[str]) -> dict[str, dict[str, int]]:
    """Size and mode of the carried files, which are present but not editable.

    These files are unbounded in size, so the census stats them rather than
    reading them: it answers whether a carried file is still the one the
    snapshot delivered, not what it contains.
    """
    result = {}
    for name in sorted(set(names)):
        path = safe_path(root, name)
        try:
            info = path.stat()
        except OSError as error:
            raise TaskError(f"carried file is missing: {name}") from error
        if not stat.S_ISREG(info.st_mode):
            raise TaskError(f"carried file must be regular: {name}")
        result[name] = {"size": info.st_size, "mode": stat.S_IMODE(info.st_mode)}
    return result


def validate_symlinks(value: object) -> dict[str, str]:
    """Strict shape validation of a request's carried-symlink inventory.

    The map binds a safe relative path to the link's literal relative target.
    Whether every entry actually resolves inside a project root is checked
    where that root exists (`verify_links` here, `confined_links` in tasks);
    this check runs wherever only the retained request is available, so a
    malformed or self-colliding inventory refuses before any tree is read or
    written. A request retained before links were carried has no field, which
    means the empty inventory.
    """
    if not isinstance(value, dict):
        raise TaskError("task symlinks must map relative paths to relative targets")
    links: dict[str, str] = {}
    folded: dict[str, str] = {}
    for name, target in value.items():
        if not isinstance(name, str) or not isinstance(target, str):
            raise TaskError("task symlinks must map relative paths to relative targets")
        parts = PurePosixPath(name).parts
        if not parts or name != PurePosixPath(name).as_posix() or name.startswith("/"):
            raise TaskError(f"invalid symlink path: {name}")
        if any(part in {".", ".."} or part.lower() in EXCLUDED for part in parts):
            raise TaskError(f"protected or escaping symlink path: {name}")
        if any(character in name for character in "\x00\n\r\\:"):
            raise TaskError(f"invalid symlink path characters: {name}")
        if not target or target.startswith("/"):
            raise TaskError(f"symlink target must be relative: {name} -> {target}")
        if any(character in target for character in "\x00\n\r\\:"):
            raise TaskError(f"invalid symlink target characters: {name} -> {target}")
        key = name.casefold()
        if key in folded:
            raise TaskError(f"duplicate symlink path: {name}")
        folded[key] = name
        links[name] = target
    for key, name in folded.items():
        parts = PurePosixPath(key).parts
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if ancestor in folded:
                raise TaskError(f"symlink {name} sits under another symlink: {folded[ancestor]}")
    return links


def within_symlink(name: str, links: dict[str, str]) -> bool:
    """Whether a change path is a carried link or reached through a linked directory.

    Changes never address a link or anything under one: the link object and its
    literal target are read-only evidence, and a linked directory's contents
    are edited only at their canonical paths.
    """
    folded = {link.casefold() for link in links}
    parts = PurePosixPath(name.casefold()).parts
    return any("/".join(parts[:index]) in folded for index in range(1, len(parts) + 1))


def check_symlink_collisions(links: dict[str, str], reserved) -> None:
    """Refuse an inventory that overlaps the snapshot's editable or carried files."""
    folded_reserved = {name.casefold(): name for name in reserved}
    for name in links:
        key = name.casefold()
        if key in folded_reserved:
            raise TaskError(f"symlink collides with a snapshot file: {name}")
        parts = PurePosixPath(key).parts
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if ancestor in folded_reserved:
                raise TaskError(
                    f"symlink {name} sits under a snapshot file: {folded_reserved[ancestor]}"
                )
        for other_key, other in folded_reserved.items():
            if other_key.startswith(key + "/"):
                raise TaskError(f"symlink directory {name} covers snapshot file {other}")


def confined_resolution(root: Path, name: str, target: str, *, excluded=lambda path: False) -> str:
    """Resolve a carried link using the filesystem's literal POSIX traversal rules."""
    if target.startswith("/"):
        raise TaskError(f"symlink target is absolute: {name} -> {target}")
    root = root.resolve()
    resolved = list(name.split("/")[:-1])
    pending = target.split("/")
    trailing_slash = target.endswith("/")
    hops = 1  # The inventory link itself counts toward Execution's link bound.
    while pending:
        part = pending.pop(0)
        if not part:
            continue
        current = root.joinpath(*resolved)
        try:
            current_info = current.stat()
        except OSError as error:
            raise TaskError(f"symlink target does not resolve: {name} -> {target}") from error
        if not stat.S_ISDIR(current_info.st_mode):
            raise TaskError(f"symlink traversal requires a directory: {name} -> {target}")
        if part == ".":
            continue
        if part == "..":
            if not resolved:
                raise TaskError(f"symlink escapes the project: {name} -> {target}")
            resolved.pop()
            continue
        candidate_parts = [*resolved, part]
        candidate = "/".join(candidate_parts)
        if part.lower() in EXCLUDED:
            raise TaskError(
                f"symlink target crosses content the snapshot excludes: {name} -> {target}"
            )
        if excluded(Path(candidate)):
            raise TaskError(f"symlink target is excluded from the snapshot: {name} -> {target}")
        path = root.joinpath(*candidate_parts)
        try:
            info = path.lstat()
        except OSError as error:
            raise TaskError(
                f"symlink does not resolve inside the project: {name} -> {target}"
            ) from error
        if stat.S_ISLNK(info.st_mode):
            hops += 1
            if hops > MAX_LINK_HOPS:
                raise TaskError(f"symlink chain is too deep: {name} -> {target}")
            following = os.readlink(path)
            if following.startswith("/"):
                raise TaskError(f"symlink target is absolute: {candidate} -> {following}")
            resolved = candidate_parts[:-1]
            # A trailing separator on any expanded link requires a directory
            # at that point, even when the outer target has more components.
            pending = [*following.split("/"), *(["."] if following.endswith("/") else []), *pending]
            continue
        resolved.append(part)
    final = root.joinpath(*resolved)
    try:
        info = final.stat()
    except OSError as error:
        raise TaskError(
            f"symlink does not resolve inside the project: {name} -> {target}"
        ) from error
    if trailing_slash and not stat.S_ISDIR(info.st_mode):
        raise TaskError(f"symlink target requires a directory: {name} -> {target}")
    return "/".join(resolved) if resolved else "."


def require_confined(root: Path, name: str, target: str, *, excluded=lambda path: False) -> str:
    """The link's confined resolution, refusing a link with no in-root target."""
    resolved = confined_resolution(root, name, target, excluded=excluded)
    if not (root / resolved).exists():
        raise TaskError(f"symlink does not resolve inside the project: {name} -> {target}")
    return resolved


def link_inventory(root: Path, *, excluded=lambda path: False) -> dict[str, str]:
    """The literal symlinks in a tree, never recursing through linked directories.

    Directories the snapshot excludes are skipped like any ordinary census. A
    kept link is returned with its literal target, and a linked directory is
    pruned rather than descended into: its contents are reached only through
    their canonical paths, which stay ordinary census subjects there.
    """
    links: dict[str, str] = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root)
        dirs[:] = [
            entry
            for entry in dirs
            if entry.lower() not in EXCLUDED and not excluded(relative / entry)
        ]
        for name in sorted([*dirs, *files]):
            path = Path(directory) / name
            if name.lower() in EXCLUDED or excluded(relative / name) or not path.is_symlink():
                continue
            links[(relative / name).as_posix()] = os.readlink(path)
            if name in dirs:
                dirs.remove(name)
    return links


def verify_links(root: Path, expected: dict[str, str]) -> None:
    """Refuse unless the tree holds exactly the retained symlink inventory.

    A missing, retargeted, replaced, added, or unsafe link refuses even when
    its resolved contents happen to match: the link object, with its literal
    target, is what the snapshot promised, not merely what it would point at.
    """
    observed = link_inventory(root)
    if observed != expected:

        def cap(names: list[str]) -> str:
            omitted = f", and {len(names) - 5} more" if len(names) > 5 else ""
            return ", ".join(names[:5]) + omitted

        detail = []
        missing = sorted(set(expected) - set(observed))
        added = sorted(set(observed) - set(expected))
        retargeted = sorted(
            name for name in set(expected) & set(observed) if expected[name] != observed[name]
        )
        if missing:
            detail.append("missing " + cap(missing))
        if retargeted:
            changed = [f"{name} -> {observed[name]} (was {expected[name]})" for name in retargeted]
            detail.append("retargeted " + cap(changed))
        if added:
            detail.append("unexpected " + cap(added))
        prefix = "snapshot symlinks differ from the retained inventory: "
        raise TaskError(prefix + "; ".join(detail))
    for name, target in sorted(expected.items()):
        require_confined(root, name, target)


def walk_files(root: Path) -> list[str]:
    """The regular files of a tree, skipping excluded and linked directories.

    A carried symlink is not an ordinary file: links are censused separately
    and never recursed through, so a file reached only through a linked
    directory never enters this listing.
    """
    names = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [
            entry
            for entry in dirs
            if entry.lower() not in EXCLUDED and not (Path(directory) / entry).is_symlink()
        ]
        for name in files:
            if (Path(directory) / name).is_symlink():
                continue
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
    # A request retained before carried files were admitted has no carried set,
    # and could have none: submit refused any project holding one outright.
    carried = request.get("carried", {})
    links = validate_symlinks(request.get("symlinks", {}))
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
        if name in carried:
            raise TaskError(f"change edits a file over the task file limit: {name}")
        if within_symlink(name, links):
            raise TaskError(f"change addresses a carried symlink: {name}")
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


def preflight_changes(root: Path, changes: list[dict]) -> None:
    """Refuse unless every destination currently holds the change's before-state."""
    for change in changes:
        path = safe_path(root, change["path"])
        before = decode(change["before"]) if change["before"] is not None else None
        actual = file_bytes(path) if path.exists() else None
        if actual != before or (
            before is not None and path.stat().st_mode & 0o777 != change["before_mode"]
        ):
            raise TaskError(f"local file changed since submission: {change['path']}")


def write_changes(root: Path, changes: list[dict]) -> None:
    """Apply preflighted changes to root, rolling back in reverse on any failure."""
    replaced = []
    created: list[Path] = []
    try:
        for change in changes:
            path = safe_path(root, change["path"])
            if change["after"] is None:
                path.unlink()
            else:
                missing = [p for p in path.parents if p.is_relative_to(root) and not p.exists()]
                for directory in reversed(missing):
                    directory.mkdir()
                    created.append(directory)
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    try:
                        stream.write(decode(change["after"]))
                        stream.close()
                        temporary.chmod(change["after_mode"])
                        temporary.replace(path)
                    except OSError:
                        temporary.unlink(missing_ok=True)
                        raise
            replaced.append(change)
    except (OSError, TaskError):
        # TaskError covers a path that turned into a symlink after preflight.
        for change in reversed(replaced):
            path = safe_path(root, change["path"])
            if change["before"] is None:
                path.unlink()
            else:
                path.write_bytes(decode(change["before"]))
                path.chmod(change["before_mode"])
        for directory in reversed(created):
            directory.rmdir()
        raise


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
