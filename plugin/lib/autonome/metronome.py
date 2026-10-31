"""The metronome: runs a project's gates over what changed, cheapest first, and decides whether
the agent may stop, must play again (encore), or is going in circles (blocked)."""

import fnmatch
import hashlib
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import common


@dataclass
class Outcome:
    gate: str
    passed: bool
    duration_ms: int
    output: str
    signature: str | None = None
    mode: str = "block"


@dataclass
class Verdict:
    status: str  # green | failing | blocked | skipped
    message: str = ""
    outcomes: list[Outcome] = field(default_factory=list)


def changed_files(cwd: str, base: str) -> list[str]:
    tracked = common.git(cwd, "diff", "--name-only", base).splitlines()
    untracked = common.git(cwd, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted(set(f for f in tracked + untracked if f))


def fingerprint(cwd: str, base: str) -> str:
    """Identifies the current state of the work, so an unchanged tree is not checked twice."""
    h = hashlib.sha256(common.git(cwd, "diff", "--binary", base).encode())
    for f in common.git(cwd, "ls-files", "--others", "--exclude-standard").splitlines():
        p = Path(cwd, f)
        if p.is_file():
            h.update(f.encode() + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def applies(gate: dict, files: list[str]) -> bool:
    patterns = gate.get("when") or []
    return not patterns or any(fnmatch.fnmatch(f, p) for f in files for p in patterns)


def signature(gate: str, output: str) -> str:
    """The shape of a failure without its noise (numbers, hashes, timings), to spot repeats."""
    tail = "\n".join(output.strip().splitlines()[-30:])
    norm = re.sub(r"\d+", "#", re.sub(r"\b[0-9a-f]{7,}\b", "h", tail))
    return hashlib.sha1(f"{gate}\n{norm}".encode()).hexdigest()[:16]


def run_gates(cwd: str, config: dict, files: list[str], only_fast: bool = False) -> list[Outcome]:
    outcomes = []
    for gate in config.get("gates", []):
        if (only_fast and not gate.get("fast")) or not applies(gate, files):
            continue
        start = time.monotonic()
        try:
            proc = subprocess.run(gate["run"], shell=True, cwd=cwd, capture_output=True, text=True,
                                  timeout=gate.get("timeout_s", 900))
            output, ok = (proc.stdout + proc.stderr)[-20000:], proc.returncode == 0
        except subprocess.TimeoutExpired:
            output, ok = f"timed out after {gate.get('timeout_s', 900)}s", False
        mode = gate.get("mode", "block")
        passed = ok or mode == "fix"
        outcomes.append(Outcome(gate["name"], passed, int((time.monotonic() - start) * 1000), output,
                                None if passed else signature(gate["name"], output), mode))
        if not passed:
            break  # cheapest first: no point paying for the rest
    return outcomes


def _state_path(session_id: str) -> Path:
    return common.STATE_DIR / f"{session_id}.json"


def load_state(session_id: str) -> dict:
    p = _state_path(session_id)
    return json.loads(p.read_text()) if p.is_file() else {}


def save_state(session_id: str, state: dict) -> None:
    common.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path(session_id).write_text(json.dumps(state))


def check(
    cwd: str, session_id: str, config: dict, *, base: str = "HEAD", config_version: int | None = None,
    task_id: int | None = None, record: bool = True,
) -> Verdict:
    """One beat of the metronome at the end of an agent turn. Keeps encore state per session."""
    state = load_state(session_id)
    files = changed_files(cwd, base)
    fp = fingerprint(cwd, base)
    if not files or fp == state.get("green_fingerprint"):
        return Verdict("skipped" if not files else "green", "nothing new to check")

    outcomes = run_gates(cwd, config, files)
    fp = fingerprint(cwd, base)  # fix-mode gates may have changed the tree
    if record and outcomes:
        try:
            common.send_json("/api/gate-runs", {
                "project": common.project_slug(cwd), "session_id": session_id, "task_id": task_id,
                "config_version": config_version,
                "runs": [{k: v for k, v in asdict(o).items() if k != "mode"} for o in outcomes],
            })
        except OSError:
            pass  # recording is best effort; the verdict below does not depend on it

    failed = next((o for o in outcomes if not o.passed), None)
    if not failed:
        save_state(session_id, {"status": "green", "green_fingerprint": fp, "attempts": 0})
        return Verdict("green", f"{len(outcomes)} gates passed", outcomes)

    encore = config.get("encore", {})
    attempts = state.get("attempts", 0)
    repeated = encore.get("stop_on_repeat", True) and failed.signature == state.get("last_signature")
    if repeated or attempts >= encore.get("max", 3):
        reason = "the same failure repeated" if repeated else f"{attempts} encores used"
        save_state(session_id, {**state, "status": "blocked", "reason": f"{failed.gate}: {reason}",
                                "last_signature": failed.signature})
        return Verdict("blocked", f"Gate '{failed.gate}' still failing ({reason}). Stopping; the task is blocked.",
                       outcomes)

    save_state(session_id, {"status": "failing", "attempts": attempts + 1, "last_signature": failed.signature})
    tail = "\n".join(failed.output.strip().splitlines()[-60:])
    return Verdict(
        "failing",
        f"Metronome: gate '{failed.gate}' failed (encore {attempts + 1}/{encore.get('max', 3)}). "
        f"Fix the cause, then finish again. Do not weaken or skip the gate.\n\n{tail}",
        outcomes,
    )


def fetch_config(cwd: str) -> dict | None:
    """{"version": int | None, "config": {...}} or None if the context is unreachable."""
    return common.get_json(f"/api/projects/{common.project_slug(cwd)}/gate-config")
