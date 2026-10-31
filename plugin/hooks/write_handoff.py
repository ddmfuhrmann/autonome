"""Writes the handoff of a finished session and sends it to autonome-context.

Runs detached from the SessionEnd hook. A separate, headless Claude reads the condensed
transcript (plus git and the plan, read-only) and writes the handoff; it is not the agent
that did the work. Standard library only, so the plugin needs no install step.
"""

import base64
import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from autonome import common  # noqa: E402

MODEL = os.getenv("AUTONOME_HANDOFF_MODEL", "sonnet")
MAX_CHARS = 150_000
TOOL_RESULT_CHARS = 400
# One source for the scribe's instructions: the handoff skill, which the evals also exercise.
SKILL = Path(__file__).resolve().parent.parent / "skills" / "handoff" / "SKILL.md"


def prompt() -> str:
    text = SKILL.read_text()
    return text.split("---", 2)[2].strip() if text.startswith("---") else text
READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)",
                   "Bash(git status:*)"]


def _tool_input(name: str, args: dict) -> str:
    for key in ("file_path", "command", "pattern", "path", "url", "description"):
        if key in args:
            return f"{name}({str(args[key])[:200]})"
    return name


def condense(transcript: Path) -> str:
    """User and assistant text in full, tool calls as one line, tool results truncated."""
    lines: list[str] = []
    for raw in transcript.read_text().splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        role = entry.get("type")
        if role not in ("user", "assistant"):
            continue
        content = entry.get("message", {}).get("content")
        if isinstance(content, str):
            lines.append(f"[{role}] {content}")
            continue
        for block in content or []:
            kind = block.get("type")
            if kind == "text" and block.get("text", "").strip():
                lines.append(f"[{role}] {block['text']}")
            elif kind == "tool_use":
                lines.append(f"[tool] {_tool_input(block.get('name', '?'), block.get('input') or {})}")
            elif kind == "tool_result":
                result = block.get("content")
                if isinstance(result, list):
                    result = " ".join(b.get("text", "") for b in result if isinstance(b, dict))
                result = str(result or "")
                if len(result) > TOOL_RESULT_CHARS:
                    result = result[:TOOL_RESULT_CHARS] + " …[truncated]"
                lines.append(f"[result] {result}")
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:  # keep the start (intent, plan) and the end (outcome)
        half = MAX_CHARS // 2
        text = text[:half] + "\n…[middle of session omitted]…\n" + text[-half:]
    return text


def gate_facts(session_id: str) -> str:
    """What the metronome recorded for this session: facts, not the agent's account of them."""
    runs = common.get_json(f"/api/gate-runs?session_id={session_id}") or []
    if not runs:
        return "No gate runs were recorded by the metronome for this session."
    lines = [f"- {r['created_at']} {r['gate']}: {'passed' if r['passed'] else 'FAILED'} ({r['duration_ms']} ms)"
             for r in reversed(runs)]
    return "Gate runs recorded by the metronome (authoritative; prefer them over the transcript):\n" + "\n".join(lines)


def write(condensed: str, cwd: str, facts: str) -> dict:
    env = {**os.environ, "AUTONOME_HANDOFF_CHILD": "1"}
    out = subprocess.run(
        ["claude", "-p", "--model", MODEL, "--output-format", "json",
         "--allowedTools", *READ_ONLY_TOOLS],
        input=prompt() + "\n\n" + facts + "\n\n" + condensed,
        cwd=cwd, env=env, capture_output=True, text=True, timeout=600,
    )
    if out.returncode != 0:
        raise RuntimeError(f"claude -p failed: {out.stderr[-2000:]}")
    result = json.loads(out.stdout)["result"]
    start, end = result.find("{"), result.rfind("}")
    return json.loads(result[start:end + 1])


def main(payload_path: str) -> None:
    event = json.loads(Path(payload_path).read_text())
    cwd = event.get("cwd") or os.getcwd()
    transcript = Path(event["transcript_path"])
    handoff = write(condense(transcript), cwd, gate_facts(event["session_id"]))
    handoff.update(project=common.project_slug(cwd), session_id=event["session_id"],
                   commit_sha=common.git(cwd, "rev-parse", "HEAD") or None)
    result = common.send_json("/api/handoffs", {
        "handoff": handoff,
        "task_id": event.get("task_id"),
        "raw_transcript_gz_b64": base64.b64encode(gzip.compress(transcript.read_bytes())).decode(),
    }, timeout=120)
    print(f"handoff stored: {result}")
    Path(payload_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main(sys.argv[1])
