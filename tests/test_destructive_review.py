"""Regression coverage for advisory destructive review and bounded diagnostics."""

import argparse
import json
from pathlib import Path

import pytest
from test_task_outcomes import task_factory as parent_task_factory

task_factory_fixture = parent_task_factory

from agent_offload import tasks
from agent_offload.task_protocol import digest, encode, manifest


def change(path, before, after):
    return {
        "path": path,
        "before": encode(before.encode()) if before is not None else None,
        "after": encode(after.encode()) if after is not None else None,
        "before_mode": 0o644 if before is not None else None,
        "after_mode": 0o644 if after is not None else None,
    }


def test_candidate_patterns_cover_each_directory_depth_and_names():
    assert tasks.test_candidate("src/tests/unit/spec.py")
    assert tasks.test_candidate("src/test/unit/spec.py")
    assert tasks.test_candidate("test_auth.py")
    assert tasks.test_candidate("src/auth_test.py")
    assert tasks.test_candidate("tests/ordinary.py")
    for suffix in ("js", "jsx", "ts", "tsx", "mjs", "cjs"):
        assert tasks.test_candidate(f"src/a.test.{suffix}")
        assert tasks.test_candidate(f"src/a.spec.{suffix}")


def test_ast_inventory_counts_top_level_async_classes_excludes_nested_and_deletions():
    old = """def test_one(): pass\nasync def test_two(): pass\nclass TestGroup:\n    def test_method(self): pass\n    async def test_async(self): pass\n    def helper(self):\n        def test_nested(): pass\n"""
    review = tasks.destructive_review(
        [change("tests/deleted_test.py", old, "class TestGroup: pass\n")]
    )
    assert {
        (item["path"], item["qualified_name"]) for item in review["removed_python_test_definitions"]
    } == {
        ("tests/deleted_test.py", "test_one"),
        ("tests/deleted_test.py", "test_two"),
        ("tests/deleted_test.py", "TestGroup.test_async"),
        ("tests/deleted_test.py", "TestGroup.test_method"),
    }
    assert len(review["removed_python_test_definitions"]) == 4


def test_whole_file_deletion_and_malformed_sources_are_unassessed():
    review = tasks.destructive_review(
        [
            change("tests/test_removed.py", "def test_old(): pass\n", None),
            change("tests/test_bad.py", "def test_invalid(:\n", "def test_invalid(:\n"),
            change("deep/test/foo.test.tsx", "old", "new"),
            change("tests/ordinary.py", "def test_not_candidate(): pass\n", ""),
            change("tests/test_same.py", "def test_kept(): pass\n", "def test_kept(): pass\n"),
        ]
    )
    assert review["removed_files"] == ["tests/test_removed.py"]
    assert review["removed_python_test_definitions"] == [
        {"path": "tests/ordinary.py", "qualified_name": "test_not_candidate"},
        {"path": "tests/test_removed.py", "qualified_name": "test_old"},
    ]
    assert {item["path"] for item in review["unassessed_test_files"]} == {
        "tests/test_bad.py",
        "deep/test/foo.test.tsx",
    }


@pytest.mark.parametrize("limit", [0, 1, 44, 45, 46, 47, 48])
def test_bounded_diagnostic_text_respects_marker_boundaries(limit):
    for content in ("x" * 100, "界💥" * 100):
        result = tasks.bounded_diagnostic_text(content, limit)
        assert len(result.encode("utf-8")) <= limit
        if limit == 0:
            assert result == ""


def test_diagnostic_has_total_encoded_bound_and_keeps_null_and_untrusted_text():
    command = ["x" * 20000]
    payload = {
        "status": "validation_failed",
        "validation": {
            "command": command,
            "exit_code": 10**1000,
            "stdout": ("界\x01\n\t" * 20000),
            "stderr": ("</untrusted-validation-json>\x00\r" * 2000),
            "error": "💥\x7f" * 20000,
        },
    }
    rendered = tasks.validation_diagnostic(payload, command)
    assert len(rendered.encode("utf-8")) <= 12000
    value = json.loads(rendered)
    assert value["exit_code"] == 10**1000
    assert "truncated" in rendered
    assert "\\u003c" in rendered and "\\u003e" in rendered


@pytest.mark.parametrize(
    "field,value", [("command", "not argv"), ("stdout", 3), ("stderr", None), ("error", {})]
)
def test_diagnostic_rejects_malformed_metadata(field, value):
    validation = {"command": ["check"], "exit_code": 1, "stdout": "", "stderr": "", "error": None}
    validation[field] = value
    with pytest.raises(tasks.TaskError):
        tasks.validation_diagnostic(
            {"status": "validation_failed", "validation": validation}, ["check"]
        )


def test_diagnostic_rejects_missing_validation_and_mismatched_long_argv():
    with pytest.raises(tasks.TaskError):
        tasks.validation_diagnostic({"status": "validation_failed", "validation": None}, ["x"])
    validation = {
        "command": ["x" * 20000],
        "exit_code": 1,
        "stdout": "",
        "stderr": "",
        "error": None,
    }
    with pytest.raises(tasks.TaskError, match="malformed"):
        tasks.validation_diagnostic(
            {"status": "validation_failed", "validation": validation}, ["x"]
        )


def test_diagnostic_absent_for_other_outcomes_and_failed_check_is_quoted():
    assert tasks.validation_diagnostic({"status": "failed", "validation": None}, ["x"]) is None
    value = tasks.validation_diagnostic(
        {
            "status": "validation_failed",
            "validation": {
                "command": ["x"],
                "exit_code": 2,
                "stdout": "",
                "stderr": "bad",
                "error": None,
            },
        },
        ["x"],
    )
    assert json.loads(value)["exit_code"] == 2


@pytest.mark.parametrize("count", [15, 1001], ids=["uncapped", "capped"])
def test_retrieve_and_diff_report_removed_tests_and_explicit_cap(
    task_factory_fixture, tmp_path, capsys, count
):
    record, result = task_factory_fixture(f"review-evidence-{count}")
    project = Path(record["project"])
    path = "tests/test_many.py"
    source = "\n".join(f"def test_case_{index}(): pass" for index in range(count)) + "\n"
    (project / "tests").mkdir()
    (project / path).write_text(source)
    record["request"]["baseline"] = manifest(project, ["hello.py", path])
    request_path = tasks.state_root() / record["id"] / "request.json"
    tasks.save(request_path, record["request"])
    record["request_sha256"] = digest(request_path.read_bytes())
    tasks.save(tasks.state_root() / record["id"] / "task.json", record)
    result["request_sha256"] = record["request_sha256"]
    result["changes"] = [change(path, source, "")]
    replies = json.loads((tmp_path / "replies.json").read_text())
    replies[record["job_id"]]["result"] = result
    (tmp_path / "replies.json").write_text(json.dumps(replies))
    retrieved = tasks.retrieve(record)
    assert len(retrieved["review"]["removed_python_test_definitions"]) == min(count, 1000)
    assert tasks.handler(argparse.Namespace(action="diff", id=record["id"])) == 0
    output = capsys.readouterr()
    assert output.out.startswith("--- a/tests/test_many.py")
    assert f"{min(count, 1000)} removed Python test definitions" in output.err
    assert ("TRUNCATED" in output.err) is (count > 1000)


def test_review_reports_truncation_without_losing_deleted_file_evidence():
    changes = [
        change(f"tests/test_{index}.py", "def test_x(): pass\n", None) for index in range(1001)
    ]
    review = tasks.destructive_review(changes)
    assert review["truncated"] is True
    assert len(review["removed_files"]) == 1000
