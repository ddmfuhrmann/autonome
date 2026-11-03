"""The gate hook: runs a project's gates over what changed, cheapest first, and decides whether
the agent may stop, must try again (retry), or is going in circles (blocked)."""

import fnmatch
import hashlib
import json
import os
import re
import signal
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
class Skipped:
    """A gate that did not run, and why; never a failure and never recorded as a gate run."""
    gate: str
    reason: str


@dataclass
class Verdict:
    status: str  # green | failing | blocked | skipped
    message: str = ""
    outcomes: list[Outcome] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


def skipped_line(s: Skipped) -> str:
    return f"{s.gate}  ({s.reason})"


def repo_root(cwd: str) -> str:
    """The root of the repo (or linked worktree) holding cwd, so gates never depend on where the
    session happens to be. Outside a repo, or if git fails, cwd itself."""
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        return cwd
    return out or cwd


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


def _untracked(cwd: str) -> set[str]:
    return set(filter(None, common.git(cwd, "ls-files", "--others", "--exclude-standard").splitlines()))


def skip_reason(gate: dict, files: list[str], only_fast: bool) -> str | None:
    if only_fast and not gate.get("fast"):
        return "not fast"
    if not applies(gate, files):
        return f"when: no changed file matches {', '.join(gate['when'])}"
    return None


KILL_GRACE_S = 5


def _run_in_group(command: str, cwd: str, timeout_s: float) -> tuple[str, bool]:
    """Runs the command in its own process group; when it ends the group is killed, and at the timeout
    it gets SIGTERM and, after a grace period, SIGKILL, so nothing it started stays running."""
    proc = subprocess.Popen(command, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        # Nothing the gate started in the background outlives it.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return out[-20000:], proc.returncode == 0
    except subprocess.TimeoutExpired:
        for sig, wait in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, None)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.communicate(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        # Survivors that detached from the group's pipes are gone with the group; reap what is left.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.kill()
        return f"timed out after {timeout_s:g}s (killed the process group)", False


def run_gates(
    cwd: str, config: dict, files: list[str], only_fast: bool = False,
) -> tuple[list[Outcome], list[Skipped]]:
    """The outcomes of the gates that ran, and the gates that did not with the reason."""
    outcomes: list[Outcome] = []
    skipped: list[Skipped] = []
    failed = False
    for gate in config.get("gates", []):
        reason = skip_reason(gate, files, only_fast)
        if reason is None and failed:
            reason = "not run: an earlier gate failed"
        if reason:
            skipped.append(Skipped(gate["name"], reason))
            continue
        start = time.monotonic()
        before = _untracked(cwd)
        output, ok = _run_in_group(gate["run"], cwd, gate.get("timeout_s", 900))
        # A gate checks the work; files it leaves behind (coverage data, reports) are not the work.
        for leftover in _untracked(cwd) - before:
            Path(cwd, leftover).unlink(missing_ok=True)
        mode = gate.get("mode", "block")
        passed = ok or mode == "fix"
        outcomes.append(Outcome(gate["name"], passed, int((time.monotonic() - start) * 1000), output,
                                None if passed else signature(gate["name"], output), mode))
        failed = not passed  # cheapest first: no point paying for the rest
    return outcomes, skipped


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
    """One run of the gate hook at the end of an agent turn. Keeps retry state per session."""
    cwd = repo_root(cwd)
    state = load_state(session_id)
    files = changed_files(cwd, base)
    fp = fingerprint(cwd, base)
    if not files or fp == state.get("green_fingerprint"):
        return Verdict("skipped" if not files else "green", "nothing new to check")

    outcomes, skipped = run_gates(cwd, config, files)
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
        return Verdict("green", f"{len(outcomes)} gates passed", outcomes, skipped)

    retries = config.get("retries") or {}
    attempts = state.get("attempts", 0)
    repeated = retries.get("stop_on_repeat", True) and failed.signature == state.get("last_signature")
    if repeated or attempts >= retries.get("max", 3):
        reason = "the same failure repeated" if repeated else f"{attempts} retries used"
        save_state(session_id, {**state, "status": "blocked", "reason": f"{failed.gate}: {reason}",
                                "last_signature": failed.signature})
        return Verdict("blocked", f"Gate '{failed.gate}' still failing ({reason}). Stopping; the task is blocked.",
                       outcomes, skipped)

    save_state(session_id, {"status": "failing", "attempts": attempts + 1, "last_signature": failed.signature})
    tail = "\n".join(failed.output.strip().splitlines()[-60:])
    note = "".join(f"\nSKIP  {skipped_line(s)}" for s in skipped)
    return Verdict(
        "failing",
        f"Gate hook: gate '{failed.gate}' failed (retry {attempts + 1}/{retries.get('max', 3)}). "
        f"Fix the cause, then finish again. Do not weaken or skip the gate.\n\n{tail}"
        + (f"\n{note}" if note else ""),
        outcomes,
        skipped,
    )


def fetch_config(cwd: str) -> dict | None:
    """{"version": int | None, "config": {...}} or None if the context is unreachable."""
    return common.get_json(f"/api/projects/{common.project_slug(cwd)}/gate-config")
