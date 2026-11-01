import asyncio
import json
from pathlib import Path

from autonome_context.server import mcp

TOOLS = Path(__file__).resolve().parent.parent / "evals" / "mocks" / "autonome" / "_tools.json"


def test_eval_mock_tool_descriptions_match_the_server():
    mocked = {t["name"]: t["description"] for t in json.loads(TOOLS.read_text())["tools"]}
    real = {t.name: t.description for t in asyncio.run(mcp.list_tools())}
    assert mocked == {name: real[name] for name in mocked}
