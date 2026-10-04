"""Generated change sets against the validate-then-apply contract.

Each example builds a baseline tree, a change set derived from it, and checks
that validation accepts it, application produces exactly the after-state,
reapplication is refused, an interrupted application leaves the tree as it
was, and tampering with a change's before-state is refused. Replay a failure
with the printed `@reproduce_failure` blob or `--hypothesis-seed`.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agent_offload.task_protocol import TaskError, encode, manifest, validate_changes
from agent_offload.tasks import apply_changes

MODES = (0o600, 0o644, 0o700, 0o755)
SEGMENTS = ("a", "b", "c.txt", "d.py", "e f.md")
PATHS = sorted({s for s in SEGMENTS} | {f"{d}/{s}" for d in ("a", "dir", "x y") for s in SEGMENTS})
CONTENT = st.text(st.characters(codec="utf-8", exclude_categories=("Cs",)), max_size=40)


def disjoint(paths: list[str]) -> list[str]:
    """Drop paths that would need another path to be both a file and a directory."""
    kept: list[str] = []
    for path in paths:
        if not any(path.startswith(f"{other}/") or other.startswith(f"{path}/") for other in kept):
            kept.append(path)
    return kept


@st.composite
def scenarios(draw):
    names = disjoint(draw(st.lists(st.sampled_from(PATHS), unique=True, max_size=8)))
    baseline = {name: (draw(CONTENT).encode(), draw(st.sampled_from(MODES))) for name in names}
    changes = []
    for name, (data, mode) in baseline.items():
        action = draw(st.sampled_from(("keep", "edit", "delete", "chmod")))
        if action == "keep":
            continue
        after, after_mode = data, mode
        if action == "edit":
            after = draw(CONTENT.map(str.encode).filter(lambda value, old=data: value != old))
        elif action == "chmod":
            after_mode = draw(st.sampled_from([m for m in MODES if m != mode]))
        changes.append(
            {
                "path": name,
                "before": encode(data),
                "after": None if action == "delete" else encode(after),
                "before_mode": mode,
                "after_mode": None if action == "delete" else after_mode,
            }
        )
    taken = list(baseline)
    for name in draw(st.lists(st.sampled_from(PATHS), unique=True, max_size=4)):
        if name in taken or disjoint([*taken, name])[-1:] != [name]:
            continue
        taken.append(name)
        changes.append(
            {
                "path": name,
                "before": None,
                "after": encode(draw(CONTENT).encode()),
                "before_mode": None,
                "after_mode": draw(st.sampled_from(MODES)),
            }
        )
    return baseline, draw(st.permutations(changes))


def materialize(root: Path, baseline: dict[str, tuple[bytes, int]]) -> None:
    for name, (data, mode) in baseline.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)


def tree(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under root with its bytes and permission bits."""
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in root.rglob("*")
        if path.is_file()
    }


def directories(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()}


def expected_after(baseline, changes) -> dict[str, tuple[bytes, int]]:
    from agent_offload.task_protocol import decode

    state = dict(baseline)
    for change in changes:
        if change["after"] is None:
            state.pop(change["path"])
        else:
            state[change["path"]] = (decode(change["after"]), change["after_mode"])
    return state


PROPERTY = settings(
    max_examples=int(os.environ.get("AGENT_OFFLOAD_PROPERTY_EXAMPLES", "150")),
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


@PROPERTY
@given(scenarios())
def test_applying_a_validated_change_set_yields_exactly_its_after_state(scenario):
    baseline, changes = scenario
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "project"
        root.mkdir()
        materialize(root, baseline)
        request = {"baseline": manifest(root, list(baseline))}
        validate_changes(request, changes)
        apply_changes(root, changes, Path(scratch) / "backup.json")
        assert tree(root) == expected_after(baseline, changes)
        if changes:
            with pytest.raises(TaskError, match="changed since submission"):
                apply_changes(root, changes, Path(scratch) / "backup-again.json")
            assert tree(root) == expected_after(baseline, changes)


@PROPERTY
@given(scenarios(), st.integers(min_value=0, max_value=20))
def test_an_interrupted_application_leaves_the_tree_as_it_was(scenario, failing_step):
    baseline, changes = scenario
    if failing_step >= len(changes):
        return
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "project"
        root.mkdir()
        materialize(root, baseline)
        before, before_directories = tree(root), directories(root)
        steps = {"count": 0, "armed": True}
        replace, unlink = Path.replace, Path.unlink

        def step(original):
            def wrapped(self, *args, **kwargs):
                if steps["armed"] and self.is_relative_to(root):
                    if steps["count"] == failing_step:
                        steps["armed"] = False
                        raise OSError("injected write failure")
                    steps["count"] += 1
                return original(self, *args, **kwargs)

            return wrapped

        with pytest.MonkeyPatch.context() as patch:
            # Forward writes go through replace (edits, creations, mode
            # changes) and unlink (deletions); each counts as one step.
            patch.setattr(Path, "replace", step(replace))
            patch.setattr(Path, "unlink", step(unlink))
            with pytest.raises(OSError, match="injected"):
                apply_changes(root, changes, Path(scratch) / "backup.json")
        assert tree(root) == before
        assert directories(root) == before_directories


@PROPERTY
@given(scenarios(), st.data())
def test_a_change_whose_before_state_was_altered_is_refused(scenario, data):
    baseline, changes = scenario
    edits = [change for change in changes if change["before"] is not None]
    if not edits:
        return
    target = data.draw(st.sampled_from(edits))
    tampered = dict(target)
    if data.draw(st.booleans()):
        tampered["before"] = encode(b"tampered " + os.urandom(4).hex().encode())
    else:
        tampered["before_mode"] = next(m for m in MODES if m != target["before_mode"])
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch) / "project"
        root.mkdir()
        materialize(root, baseline)
        request = {"baseline": manifest(root, list(baseline))}
        forged = [tampered if change is target else change for change in changes]
        with pytest.raises(TaskError, match="disagrees with baseline"):
            validate_changes(request, forged)
