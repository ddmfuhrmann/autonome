"""Run with: uv run --project context pytest plugin/tests"""

import json
import runpy
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from autonome import common, gate_hook  # noqa: E402

GATES_BIN = Path(__file__).resolve().parent.parent / "bin" / "autonome-gates"

CONFIG = {
    "gates": [
        {"name": "format", "run": "sed -i.bak 's/  */ /g' app.txt && rm -f app.txt.bak", "mode": "fix", "fast": True},
        {"name": "lint", "run": "! grep -q TODO app.txt", "fast": True},
        {"name": "docs-only", "run": "false", "when": ["docs/*"]},
        {"name": "test", "run": "grep -q ok app.txt || (echo \"expected ok at $(date +%s)\"; exit 1)"},
    ],
    "retries": {"max": 2, "stop_on_repeat": True},
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


def check(repo, session, config=CONFIG):
    return gate_hook.check(str(repo), session, config, record=False)


def test_gate_runs_at_the_repo_root_when_cwd_is_a_subfolder(repo):
    (repo / "context").mkdir()
    (repo / "context" / "marker").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "sub"], cwd=repo, check=True)
    (repo / "app.txt").write_text("changed\n")
    config = {"gates": [{"name": "sub", "run": "cd context && test -f marker"}], "retries": {"max": 2}}
    v = gate_hook.check(str(repo / "context"), "s", config, record=False)
    assert v.status == "green" and [o.gate for o in v.outcomes] == ["sub"]


def test_repo_root_of_a_worktree_is_the_worktree(repo, tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "other", str(wt)], cwd=repo, check=True)
    (wt / "deep").mkdir()
    assert Path(gate_hook.repo_root(str(wt / "deep"))).resolve() == wt.resolve()
    assert Path(gate_hook.repo_root(str(repo))).resolve() == repo.resolve()


def test_repo_root_outside_a_repo_is_the_cwd(tmp_path):
    assert gate_hook.repo_root(str(tmp_path)) == str(tmp_path)
    assert gate_hook.repo_root(str(tmp_path / "missing")) == str(tmp_path / "missing")


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
    assert "retry 1/2" in v.message


def test_same_failure_twice_blocks_even_with_noise_in_output(repo):
    session = uuid.uuid4().hex
    (repo / "app.txt").write_text("bad\n")
    assert check(repo, session).status == "failing"
    (repo / "app.txt").write_text("still bad\n")
    v = check(repo, session)
    assert v.status == "blocked" and "same failure" in v.message


def test_different_failures_use_up_retries(repo):
    session = uuid.uuid4().hex
    for i, text in enumerate(["TODO\n", "bad\n"]):
        (repo / "app.txt").write_text(text)
        assert check(repo, session).status == "failing"
    (repo / "app.txt").write_text("TODO again\n")
    v = check(repo, session)
    assert v.status == "blocked" and "2 retries used" in v.message


def test_gate_config_without_retries_uses_the_default(repo):
    config = {"gates": CONFIG["gates"]}
    session = uuid.uuid4().hex
    (repo / "app.txt").write_text("TODO\n")
    v = check(repo, session, config)
    assert v.status == "failing" and "retry 1/3" in v.message


def test_signature_ignores_numbers_and_hashes():
    a = gate_hook.signature("test", "failed at 1730000000 in 0.52s commit abcdef1234")
    b = gate_hook.signature("test", "failed at 1730000999 in 1.07s commit 1234abcdef")
    assert a == b and a != gate_hook.signature("lint", "failed at 1730000000 in 0.52s commit abcdef1234")


def test_files_a_gate_leaves_behind_are_removed_but_the_agents_are_kept(repo):
    (repo / "new.txt").write_text("written by the agent\n")
    config = {"gates": [{"name": "test", "run": "echo data > .coverage && echo xml > report.xml"}]}
    outcomes, _ = gate_hook.run_gates(str(repo), config, ["new.txt"])
    assert outcomes[0].passed
    assert (repo / "new.txt").exists()
    assert not (repo / ".coverage").exists() and not (repo / "report.xml").exists()


def run_cli(repo, monkeypatch, capsys, *argv, config):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["autonome-gates", *argv])
    monkeypatch.setattr(gate_hook, "fetch_config", lambda cwd: {"version": 1, "config": config})
    with pytest.raises(SystemExit) as e:
        runpy.run_path(str(GATES_BIN), run_name="__main__")
    return e.value.code, capsys.readouterr().out


def test_fast_prints_passes_and_skips_with_the_reason(repo, monkeypatch, capsys):
    (repo / "app.txt").write_text("changed\n")
    ui = "context/templates/**"
    config = {"gates": [
        {"name": "a", "run": "true", "fast": True},
        {"name": "b", "run": "true", "fast": True},
        {"name": "ui-dates", "run": "false", "fast": True, "when": [ui]},
        {"name": "ui-more", "run": "false", "fast": True, "when": [ui, "x/*"]},
        {"name": "slow", "run": "false"},
    ]}
    code, out = run_cli(repo, monkeypatch, capsys, "--fast", config=config)
    lines = out.splitlines()
    assert code == 0
    assert [ln.split()[:2] for ln in lines if ln.startswith("PASS")] == [["PASS", "a"], ["PASS", "b"]]
    assert f"SKIP  ui-dates  (when: no changed file matches {ui})" in lines
    assert f"SKIP  ui-more  (when: no changed file matches {ui}, x/*)" in lines
    assert "SKIP  slow  (not fast)" in lines


def test_gates_after_a_failure_are_skipped_and_json_lists_them(repo, monkeypatch, capsys):
    (repo / "app.txt").write_text("changed\n")
    config = {"gates": [{"name": "a", "run": "false"}, {"name": "b", "run": "true"}]}
    code, out = run_cli(repo, monkeypatch, capsys, config=config)
    assert code == 1 and "SKIP  b  (not run: an earlier gate failed)" in out
    code, out = run_cli(repo, monkeypatch, capsys, "--json", config=config)
    data = json.loads(out)
    assert data["skipped"] == [{"gate": "b", "reason": "not run: an earlier gate failed"}]
    assert [o["gate"] for o in data["outcomes"]] == ["a"]


def test_skipped_gates_are_not_recorded_and_show_in_the_hook_message(repo):
    (repo / "app.txt").write_text("TODO\n")
    v = check(repo, "s")
    assert v.status == "failing" and [s.gate for s in v.skipped] == ["docs-only", "test"]
    assert "SKIP  test  (not run: an earlier gate failed)" in v.message
    assert [o.gate for o in v.outcomes] == ["format", "lint"]


def _alive(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def test_a_gate_that_times_out_dies_with_its_whole_process_group(tmp_path):
    import time

    marker = f"sleep 3{uuid.uuid4().int % 900 + 100}"
    config = {"gates": [{"name": "slow", "run": f"sh -c '{marker} & wait'", "timeout_s": 1}]}

    start = time.monotonic()
    outcomes, _ = gate_hook.run_gates(str(tmp_path), config, [])

    assert time.monotonic() - start < 10
    assert not outcomes[0].passed
    assert "timed out after 1s (killed the process group)" in outcomes[0].output
    assert not _alive(marker)


def test_a_timed_out_gate_leaves_processes_outside_its_group_alone(tmp_path):
    marker = f"sleep 4{uuid.uuid4().int % 900 + 100}"
    bystander = subprocess.Popen(marker.split(), start_new_session=True)
    try:
        config = {"gates": [{"name": "slow", "run": "sleep 60", "timeout_s": 1}]}
        gate_hook.run_gates(str(tmp_path), config, [])
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_a_gate_that_exits_does_not_leave_background_processes(tmp_path):
    marker = f"sleep 5{uuid.uuid4().int % 900 + 100}"
    config = {"gates": [{"name": "bg", "run": f"{marker} >/dev/null 2>&1 &", "timeout_s": 30}]}

    outcomes, _ = gate_hook.run_gates(str(tmp_path), config, [])

    assert outcomes[0].passed
    assert not _alive(marker)


def test_a_gate_that_ignores_sigterm_is_killed_after_the_grace_period(tmp_path):
    import time

    marker = f"sleep 6{uuid.uuid4().int % 900 + 100}"
    config = {"gates": [{"name": "stubborn", "run": f"trap '' TERM; {marker}", "timeout_s": 1}]}

    start = time.monotonic()
    outcomes, _ = gate_hook.run_gates(str(tmp_path), config, [])

    assert 5 <= time.monotonic() - start < 15
    assert "timed out after 1s (killed the process group)" in outcomes[0].output
    assert not _alive(marker)


def test_a_task_runs_the_gates_it_brings_before_they_join_the_config(monkeypatch):
    fetched = {"version": 3, "config": {"gates": [{"name": "lint", "run": "ruff check"}]},
               "pending": [{"name": "typecheck", "run": "uv run mypy", "fast": True, "task_id": 1801, "plan_id": 1792},
                           {"name": "architecture", "run": "lint-imports", "task_id": 1802, "plan_id": 1792},
                           {"name": "lint", "run": "other", "task_id": 1801, "plan_id": 1792}]}
    monkeypatch.setattr(gate_hook.common, "get_json", lambda path: fetched)
    monkeypatch.setattr(gate_hook.common, "project_slug", lambda cwd: "p")
    monkeypatch.delenv("AUTONOME_TASK_ID", raising=False)
    assert gate_hook.fetch_config("/x") is fetched
    monkeypatch.setenv("AUTONOME_TASK_ID", "1801")
    got = gate_hook.fetch_config("/x")
    assert got["config"]["gates"] == [{"name": "lint", "run": "ruff check"},
                                      {"name": "typecheck", "run": "uv run mypy", "fast": True}]
    assert got["version"] == 3
    monkeypatch.setenv("AUTONOME_TASK_ID", "9")
    assert gate_hook.fetch_config("/x")["config"]["gates"] == [{"name": "lint", "run": "ruff check"}]
