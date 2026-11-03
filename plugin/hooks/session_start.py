"""SessionStart hook: tells the agent what the project's memory knows.

In a runner session (AUTONOME_TASK_ID set) it injects the task, its plan, the conventions for the
task's layers and earlier friction in the feature, and the paths it names that are not in the
worktree. Otherwise it injects a short pointer to the memory. Silent when the context is down or the project is not set up for autonome.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from autonome import common  # noqa: E402


def main() -> None:
    if os.getenv("AUTONOME_HANDOFF_CHILD"):
        return
    event = json.load(sys.stdin)
    cwd = event.get("cwd") or os.getcwd()
    task_id = os.getenv("AUTONOME_TASK_ID")
    if not task_id and not common.project_active(cwd):
        return
    query = f"?task_id={task_id}" if task_id else ""
    result = common.get_json(f"/api/projects/{common.project_slug(cwd)}/briefing{query}", timeout=3)
    if not result or not result.get("text"):
        return
    text = result["text"]
    if task_id:
        text = "\n\n".join(filter(None, [text, common.missing_paths_note(common.missing_paths(text, cwd))]))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"autonome: {e}", file=sys.stderr)
    sys.exit(0)
