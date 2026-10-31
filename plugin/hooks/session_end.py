"""SessionEnd hook: hands the finished session to write_handoff.py in the background.

Never blocks or fails the session. Skips when the context is down, when the session was
trivial, and inside the handoff writer's own session (no recursion).
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

CONTEXT_URL = os.getenv("AUTONOME_CONTEXT_URL", "http://127.0.0.1:8765")
MIN_USER_TURNS = int(os.getenv("AUTONOME_MIN_USER_TURNS", "3"))
LOG_DIR = Path.home() / ".autonome" / "logs"


def user_turns(transcript: Path) -> int:
    n = 0
    with transcript.open() as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = entry.get("message", {}).get("content")
            if entry.get("type") == "user" and (
                isinstance(content, str)
                or any(b.get("type") == "text" for b in content or [] if isinstance(b, dict))
            ):
                n += 1
    return n


def context_up() -> bool:
    try:
        with urllib.request.urlopen(f"{CONTEXT_URL}/health", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def main() -> None:
    if os.getenv("AUTONOME_HANDOFF_CHILD"):
        return
    event = json.load(sys.stdin)
    transcript = Path(event.get("transcript_path", ""))
    if not transcript.is_file() or user_turns(transcript) < MIN_USER_TURNS or not context_up():
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, dir=LOG_DIR)
    json.dump(event, payload)
    payload.close()
    log = (LOG_DIR / f"{event.get('session_id', 'session')}.log").open("a")
    subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("write_handoff.py")), payload.name],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True, cwd=event.get("cwd") or None,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # a hook must never break the user's session
        print(f"autonome: {e}", file=sys.stderr)
    sys.exit(0)
