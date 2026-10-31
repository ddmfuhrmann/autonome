"""Shared helpers for the plugin's hooks and scripts. Standard library only."""

import json
import os
import subprocess
import time
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
    """AUTONOME_PROJECT, else the repo's folder name. In a linked worktree (the maestro's) that is the
    main checkout's folder, not the worktree's, so its sessions land in the right project."""
    if os.getenv("AUTONOME_PROJECT"):
        return os.environ["AUTONOME_PROJECT"]
    common_dir = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common_dir.endswith("/.git"):
        return Path(common_dir).parent.name
    return Path(git(cwd, "rev-parse", "--show-toplevel") or cwd).name


# Eval runs (claude plugin eval) set this so hooks never reach a real context.
OFFLINE = bool(os.getenv("EVAL_AUTONOME_OFFLINE"))


# Seconds to wait for a restarting context before giving up. Hooks keep 0: they must never hang a
# session. Long-running scripts (autonome-docs) set it from AUTONOME_CONTEXT_WAIT_S.
WAIT_S = 0.0


def wait_for_context(limit_s: float) -> bool:
    """Polls /health with backoff until the context answers or the limit passes."""
    deadline, delay = time.monotonic() + limit_s, 1.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{CONTEXT_URL}/health", timeout=5):
                return True
        except OSError:
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, 15)
    return False


def _unreachable(e: Exception) -> bool:
    return isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError) \
        or isinstance(e, ConnectionError)


def get_json(path: str, timeout: float = 5):
    """None when the context is unreachable or answers with an error."""
    if OFFLINE:
        return None
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(f"{CONTEXT_URL}{path}", timeout=timeout) as r:
                return json.loads(r.read())
        except (OSError, json.JSONDecodeError) as e:
            if attempt == 2 or not _unreachable(e) or not WAIT_S or not wait_for_context(WAIT_S):
                return None
    return None


def send_json(path: str, data, method: str = "POST", timeout: float = 30):
    if OFFLINE:
        raise OSError("autonome is offline (eval run)")
    req = urllib.request.Request(
        f"{CONTEXT_URL}{path}", data=json.dumps(data, default=str).encode(), method=method,
        headers={"Content-Type": "application/json"},
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                return json.loads(body) if body else None
        except OSError as e:
            if attempt == 2 or not _unreachable(e) or not WAIT_S or not wait_for_context(WAIT_S):
                raise


def role_model(project: str, role: str, default_model: str = "sonnet", default_effort: str = "low") -> tuple[str, str]:
    """Model and effort for a role: AUTONOME_<ROLE>_MODEL/_EFFORT, else the gate config, else the default."""
    cfg = (get_json(f"/api/projects/{project}/gate-config") or {}).get("config") or {}
    r = (cfg.get("models") or {}).get(role) or {}
    return (os.getenv(f"AUTONOME_{role.upper()}_MODEL") or r.get("model") or default_model,
            os.getenv(f"AUTONOME_{role.upper()}_EFFORT") or r.get("effort") or default_effort)


def project_active(cwd: str | Path) -> bool:
    """autonome only acts in projects set up for it: AUTONOME_PROJECT is set, the project has a gate
    config saved in the context (the setup step, in the UI), or an approved plan brings gates. Anywhere
    else its hooks stay silent and spend nothing: no briefing, no handoff."""
    if os.getenv("AUTONOME_PROJECT"):
        return True
    cfg = get_json(f"/api/projects/{project_slug(cwd)}/gate-config", timeout=2)
    return bool(cfg and (cfg.get("version") is not None or cfg.get("pending")))
