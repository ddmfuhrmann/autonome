"""Run with: uv run --project context pytest plugin/tests"""

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from autonome import common, metronome  # noqa: E402

CONFIG = {
    "gates": [
        {"name": "format", "run": "sed -i.bak 's/  */ /g' app.txt && rm -f app.txt.bak", "mode": "fix", "fast": True},
        {"name": "lint", "run": "! grep -q TODO app.txt", "fast": True},
        {"name": "docs-only", "run": "false", "when": ["docs/*"]},
        {"name": "test", "run": "grep -q ok app.txt || (echo \"expected ok at $(date +%s)\"; exit 1)"},
    ],
    "encore": {"max": 2, "stop_on_repeat": True},
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", tmp_path / "state")
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    (r / "app.txt").write_text("start\n")
    subprocess.run(["git", "add", "."], cwd=r, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], cwd=r, check=True)
    return r


def check(repo, session):
    return metronome.check(str(repo), session, CONFIG, record=False)


def test_nothing_changed_is_skipped(repo):
    assert check(repo, "s").status == "skipped"


def test_green_runs_fix_gates_and_skips_unmatched_paths(repo):
    (repo / "app.txt").write_text("all   ok\n")
    v = check(repo, "s")
    assert v.status == "green"
    assert [o.gate for o in v.outcomes] == ["format", "lint", "test"]
    assert (repo / "app.txt").read_text() == "all ok\n"
    assert check(repo, "s").message == "nothing new to check"


def test_cheapest_failure_stops_the_rest(repo):
    (repo / "app.txt").write_text("TODO ok\n")
    v = check(repo, "s")
    assert v.status == "failing" and [o.gate for o in v.outcomes] == ["format", "lint"]
    assert "encore 1/2" in v.message


def test_same_failure_twice_blocks_even_with_noise_in_output(repo):
    session = uuid.uuid4().hex
    (repo / "app.txt").write_text("bad\n")
    assert check(repo, session).status == "failing"
    (repo / "app.txt").write_text("still bad\n")
    v = check(repo, session)
    assert v.status == "blocked" and "same failure" in v.message


def test_different_failures_use_up_encores(repo):
    session = uuid.uuid4().hex
    for i, text in enumerate(["TODO\n", "bad\n"]):
        (repo / "app.txt").write_text(text)
        assert check(repo, session).status == "failing"
    (repo / "app.txt").write_text("TODO again\n")
    assert check(repo, session).status == "blocked"


def test_signature_ignores_numbers_and_hashes():
    a = metronome.signature("test", "failed at 1730000000 in 0.52s commit abcdef1234")
    b = metronome.signature("test", "failed at 1730000999 in 1.07s commit 1234abcdef")
    assert a == b and a != metronome.signature("lint", "failed at 1730000000 in 0.52s commit abcdef1234")
