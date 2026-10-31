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
