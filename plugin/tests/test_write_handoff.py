"""Run with: uv run --project context pytest plugin/tests"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import write_handoff  # noqa: E402
from autonome import common  # noqa: E402

CATALOG = [
    {"slug": "runner", "description": "Runs approved tasks", "paths": ["plugin/runner/src/**"]},
    {"slug": "triage", "description": "Judges deviations", "paths": ["plugin/runner/src/triage.ts"]},
    {"slug": "old", "description": None, "paths": []},
]


def _captured_prompt(monkeypatch, features) -> str:
    seen = {}

    def fake_run(cmd, input, **kwargs):
        seen["input"] = input
        return subprocess.CompletedProcess(cmd, 0, stdout='{"result": "{}"}', stderr="")

    monkeypatch.setattr(common, "get_json", lambda path, timeout=5: features)
    monkeypatch.setattr(common, "project_slug", lambda cwd: "autonome")
    monkeypatch.setattr(common, "model_args", lambda project, role: [])
    monkeypatch.setattr(common, "language_rule", lambda project: "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    write_handoff.write("[user] hi", "/tmp", "facts")
    return seen["input"]


def test_prompt_includes_the_catalog_when_the_api_answers(monkeypatch):
    prompt = _captured_prompt(monkeypatch, CATALOG)

    assert "- runner: Runs approved tasks (paths: plugin/runner/src/**)" in prompt
    assert "- triage: Judges deviations (paths: plugin/runner/src/triage.ts)" in prompt
    assert "- old:" not in prompt


def test_prompt_says_no_catalog_when_the_api_is_out(monkeypatch):
    prompt = _captured_prompt(monkeypatch, None)

    assert "Feature catalog: no catalog available." in prompt
    assert "[user] hi" in prompt
