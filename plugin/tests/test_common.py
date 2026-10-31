import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, str(BIN.parent / "lib"))


def test_docs_report_an_unreachable_context_in_one_line_after_waiting(tmp_path):
    env = {**os.environ, "AUTONOME_CONTEXT_URL": "http://127.0.0.1:9", "AUTONOME_CONTEXT_WAIT_S": "1"}
    start = time.monotonic()
    out = subprocess.run([sys.executable, BIN / "autonome-docs", "--project", "p", "--feature", "links"],
                         cwd=tmp_path, env=env, capture_output=True, text=True)
    assert out.returncode == 1 and time.monotonic() - start >= 1
    assert "Traceback" not in out.stderr and out.stderr.strip().count("\n") == 0
    assert out.stderr.startswith("autonome-docs:")


def test_send_json_waits_for_a_restarting_context_and_retries(monkeypatch):
    from autonome import common

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200), self.end_headers(), self.wfile.write(b'{"status": "ok"}')

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))  # an unread body makes the close a reset
            self.send_response(200), self.end_headers(), self.wfile.write(b'{"saved": true}')

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    server.server_close()  # down for now; it comes back below
    monkeypatch.setattr(common, "CONTEXT_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(common, "WAIT_S", 10.0)

    def come_back():
        time.sleep(1.5)
        s = HTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=s.serve_forever, daemon=True).start()

    threading.Thread(target=come_back, daemon=True).start()
    assert common.send_json("/api/x", {"a": 1}) == {"saved": True}


def test_a_session_in_a_maestro_worktree_belongs_to_the_main_checkouts_project(tmp_path, monkeypatch):
    from autonome import common

    monkeypatch.delenv("AUTONOME_PROJECT", raising=False)
    repo = tmp_path / "shortlink"
    repo.mkdir()
    run = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    worktree = tmp_path / ".autonome-worktrees" / "autonome-task-22-abc"
    run("worktree", "add", "-q", "-b", "task", str(worktree))
    assert common.project_slug(repo) == "shortlink"
    assert common.project_slug(worktree) == "shortlink"
    assert common.project_slug(worktree / ".") == "shortlink"


def test_scribes_take_model_and_effort_from_env_then_gate_config_then_default(monkeypatch):
    from autonome import common

    monkeypatch.setattr(common, "get_json", lambda path, timeout=5: {"config": {"models": {"handoff": {"model": "haiku", "effort": "low"}}}})
    assert common.role_model("p", "handoff") == ("haiku", "low")
    assert common.role_model("p", "docs") == ("sonnet", "low")
    monkeypatch.setenv("AUTONOME_HANDOFF_EFFORT", "medium")
    assert common.role_model("p", "handoff") == ("haiku", "medium")


def _fake_context(gate_versions: dict[str, int | None]):
    """A context that knows which projects have a saved gate config, and records briefing calls."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            parts = self.path.split("/")
            body = b'{"status": "ok"}'
            if self.path.endswith("/gate-config"):
                body = ('{"version": %s, "config": {}}' % json.dumps(gate_versions.get(parts[3]))).encode()
            elif "/briefing" in self.path:
                body = b'{"text": "autonome: memory pointer"}'
            self.send_response(200), self.end_headers(), self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", calls, server


def _repo(tmp_path, name):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    return repo


def _hook(name, repo, url, event, home):
    env = {k: v for k, v in os.environ.items() if not k.startswith("AUTONOME_")}
    env.update(AUTONOME_CONTEXT_URL=url, AUTONOME_HOME=str(home))
    return subprocess.run([sys.executable, BIN.parent / "hooks" / name], input=json.dumps(event), cwd=repo,
                          env=env, capture_output=True, text=True)


def test_hooks_stay_silent_in_projects_not_set_up_for_autonome(tmp_path):
    url, calls, server = _fake_context({"shortlink": 3, "confere-v2": None})
    try:
        for name, active in [("shortlink", True), ("confere-v2", False)]:
            repo = _repo(tmp_path, name)
            out = _hook("session_start.py", repo, url, {"cwd": str(repo)}, tmp_path / "home")
            assert ("memory pointer" in out.stdout) is active, name
        assert not any("/projects/confere-v2/briefing" in c for c in calls)
    finally:
        server.shutdown()


def test_no_handoff_is_written_for_a_project_not_set_up(tmp_path):
    url, calls, server = _fake_context({"confere-v2": None})
    try:
        repo = _repo(tmp_path, "confere-v2")
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("\n".join(json.dumps({"type": "user", "message": {"content": f"turn {i}"}}) for i in range(10)))
        home = tmp_path / "home"
        _hook("session_end.py", repo, url, {"cwd": str(repo), "session_id": "s", "transcript_path": str(transcript)}, home)
        assert not (home / "logs").exists()  # the scribe was never started
    finally:
        server.shutdown()
