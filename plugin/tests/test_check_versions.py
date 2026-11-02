import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-versions"


def make_repo(root: Path, plugin="0.1.0", market="0.1.0", name="autonome"):
    (root / "plugin/.claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / "plugin/.claude-plugin/plugin.json").write_text(
        json.dumps({"name": "autonome", "version": plugin})
    )
    (root / ".claude-plugin/marketplace.json").write_text(
        json.dumps({"plugins": [{"name": name, "version": market}]})
    )


def run(root: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)], capture_output=True, text=True
    )


def test_equal_versions_exit_zero_silently(tmp_path):
    make_repo(tmp_path)
    r = run(tmp_path)
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == ""


def test_different_versions_exit_one_with_both(tmp_path):
    make_repo(tmp_path, plugin="0.25.1", market="0.25.0")
    r = run(tmp_path)
    assert r.returncode == 1
    assert r.stdout.strip() == "plugin.json 0.25.1 != marketplace.json 0.25.0: bump both"


def test_plugin_missing_from_marketplace(tmp_path):
    make_repo(tmp_path, name="other")
    r = run(tmp_path)
    assert r.returncode == 1
    assert "Traceback" not in r.stdout + r.stderr
    assert len(r.stdout.strip().splitlines()) == 1


@pytest.mark.parametrize("plugins", [["autonome"], {}, "autonome", None])
def test_malformed_plugins_entry(tmp_path, plugins):
    make_repo(tmp_path)
    (tmp_path / ".claude-plugin/marketplace.json").write_text(
        json.dumps({"plugins": plugins})
    )
    r = run(tmp_path)
    assert r.returncode == 1
    assert "Traceback" not in r.stdout + r.stderr
    assert len(r.stdout.strip().splitlines()) == 1


def test_invalid_json(tmp_path):
    make_repo(tmp_path)
    (tmp_path / ".claude-plugin/marketplace.json").write_text("{nope")
    r = run(tmp_path)
    assert r.returncode == 1
    assert "Traceback" not in r.stdout + r.stderr
    assert len(r.stdout.strip().splitlines()) == 1


def test_missing_file(tmp_path):
    make_repo(tmp_path)
    (tmp_path / "plugin/.claude-plugin/plugin.json").unlink()
    r = run(tmp_path)
    assert r.returncode == 1
    assert "Traceback" not in r.stdout + r.stderr
    assert len(r.stdout.strip().splitlines()) == 1


def test_repo_versions_are_aligned():
    root = SCRIPT.parent.parent
    assert run(root).returncode == 0
