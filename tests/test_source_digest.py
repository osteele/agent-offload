from pathlib import Path

from agent_offload.protocol import source_sha256


def test_package_digest_ignores_relay_dependencies_and_hidden_files(tmp_path: Path) -> None:
    module = tmp_path / "protocol.py"
    module.write_text("original\n")
    before = source_sha256(tmp_path)

    dependency = tmp_path / "relay_runtime" / "node_modules" / "x" / "y.py"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("installed\n")
    assert source_sha256(tmp_path) == before

    hidden = tmp_path / ".hidden" / "z.py"
    hidden.parent.mkdir()
    hidden.write_text("hidden\n")
    assert source_sha256(tmp_path) == before

    module.write_text("changed\n")
    assert source_sha256(tmp_path) != before
