"""Run with: uv run --project context pytest plugin/tests"""

import io
import runpy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from autonome import gate_hook  # noqa: E402

STOP = Path(__file__).resolve().parent.parent / "hooks" / "stop.py"


def test_main_failure_prints_message_with_space_and_exits_zero(monkeypatch, capsys):
    def boom(cwd):
        raise RuntimeError("boom")

    monkeypatch.delenv("AUTONOME_HANDOFF_CHILD", raising=False)
    monkeypatch.setattr(gate_hook, "fetch_config", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id": "s1", "cwd": "/tmp"}'))

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(STOP), run_name="__main__")

    assert exit_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == "autonome gate hook: boom\n"
    assert captured.out == ""
