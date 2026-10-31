"""Shared helpers for the plugin's hooks and scripts. Standard library only."""

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

CONTEXT_URL = os.getenv("AUTONOME_CONTEXT_URL", "http://127.0.0.1:8765")
HOME = Path(os.getenv("AUTONOME_HOME", Path.home() / ".autonome"))
STATE_DIR = HOME / "state"
LOG_DIR = HOME / "logs"


def git(cwd: str | Path, *args: str, check: bool = False) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)
    return out.stdout.strip()


def project_slug(cwd: str | Path) -> str:
    if os.getenv("AUTONOME_PROJECT"):
        return os.environ["AUTONOME_PROJECT"]
    return Path(git(cwd, "rev-parse", "--show-toplevel") or cwd).name


def get_json(path: str, timeout: float = 5):
    """None when the context is unreachable or answers with an error."""
    try:
        with urllib.request.urlopen(f"{CONTEXT_URL}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError):
        return None


def send_json(path: str, data, method: str = "POST", timeout: float = 30):
    req = urllib.request.Request(
        f"{CONTEXT_URL}{path}", data=json.dumps(data, default=str).encode(), method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else None
