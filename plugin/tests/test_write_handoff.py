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
    monkeypatch.setattr(common, "record_spend", lambda *a, **k: None)
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


def test_task_diff_fact_names_the_diff_from_the_base():
    fact = write_handoff.task_diff_fact("abc123")

    assert "git diff abc123..HEAD" in fact
    assert "git diff --name-only abc123..HEAD" in fact


def test_task_diff_fact_is_empty_without_a_base():
    assert write_handoff.task_diff_fact(None) == ""
    assert write_handoff.task_diff_fact("") == ""


def _facts_sent(monkeypatch, tmp_path, event_extra) -> str:
    import json

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    payload = tmp_path / "p.json"
    payload.write_text(json.dumps({"session_id": "s", "transcript_path": str(transcript), "cwd": str(tmp_path), **event_extra}))
    seen = {}

    def fake_write(condensed, cwd, facts, task_id=None):
        seen["facts"] = facts
        return {}

    monkeypatch.setattr(write_handoff, "write", fake_write)
    monkeypatch.setattr(write_handoff, "gate_facts", lambda sid: "gate facts")
    monkeypatch.setattr(common, "project_slug", lambda cwd: "autonome")
    monkeypatch.setattr(common, "git", lambda cwd, *a: "head")
    monkeypatch.setattr(common, "send_json", lambda *a, **k: {})
    write_handoff.main(str(payload))
    return seen["facts"]


def test_main_adds_the_task_diff_to_the_facts_when_the_payload_has_a_base(monkeypatch, tmp_path):
    facts = _facts_sent(monkeypatch, tmp_path, {"base": "abc123"})

    assert facts.startswith("gate facts")
    assert "git diff abc123..HEAD" in facts


def test_main_sends_only_the_gate_facts_without_a_base(monkeypatch, tmp_path):
    assert _facts_sent(monkeypatch, tmp_path, {}) == "gate facts"
