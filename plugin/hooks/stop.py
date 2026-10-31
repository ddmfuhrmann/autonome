"""Stop hook: the metronome. Exit code 2 sends the failure back to the agent and keeps it working.

Runs in conduct sessions (AUTONOME_CONDUCT=1), and in interactive ones only when the project's
gate config says interactive: true. In conduct it fails closed: no context, no green light.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from autonome import metronome  # noqa: E402


def main() -> int:
    if os.getenv("AUTONOME_HANDOFF_CHILD"):
        return 0
    event = json.load(sys.stdin)
    cwd, session_id = event.get("cwd") or os.getcwd(), event["session_id"]
    conduct = os.getenv("AUTONOME_CONDUCT") == "1"

    fetched = metronome.fetch_config(cwd)
    if fetched is None:
        if conduct:
            metronome.save_state(session_id, {"status": "blocked", "reason": "context unreachable"})
        return 0
    if not conduct and not fetched["config"].get("interactive"):
        return 0

    verdict = metronome.check(
        cwd, session_id, fetched["config"],
        base=os.getenv("AUTONOME_BASE_SHA", "HEAD"), config_version=fetched["version"],
        task_id=int(os.environ["AUTONOME_TASK_ID"]) if os.getenv("AUTONOME_TASK_ID") else None,
    )
    if verdict.status == "failing":
        print(verdict.message, file=sys.stderr)
        return 2
    if verdict.status == "blocked":
        print(verdict.message, file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # a broken hook must not trap the session
        print(f"autonome metronome: {e}", file=sys.stderr)
        sys.exit(0)
