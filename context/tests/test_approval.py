"""Approving in the session: the MCP server asks the human through the client (elicitation)."""

import asyncio
import json
import socket
import threading
import time

import psycopg_pool
import pytest
import uvicorn
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from autonome_context import plans, services, store
from autonome_context.plans import PlanBody, ProposedGate, TaskSpec
from autonome_context.server import build_app


@pytest.fixture
def server(db_url, indexer, monkeypatch):
    pools = []

    async def get():  # one pool, opened on the server's own event loop
        if not pools:
            pools.append(psycopg_pool.AsyncConnectionPool(db_url, open=False, min_size=1, max_size=4))
            await pools[0].open()
        return pools[0], indexer

    monkeypatch.setattr(services, "get", get)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True


def call(url, tool, args, answer):
    """Calls a tool as a client whose human answers the elicitation with `answer` (None: cannot ask)."""
    asked = []

    async def elicit(context, params):
        asked.append(params.message)
        return types.ElicitResult(action=answer, content={"approve": True} if answer == "accept" else None)

    async def go():
        async with streamable_http_client(url) as (read, write, *_):
            async with ClientSession(read, write, elicitation_callback=elicit if answer else None) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
                return json.loads(result.content[0].text)

    return asyncio.run(go()), asked


@pytest.fixture
def draft(run_db, project, indexer):
    async def go(conn):
        plan_id = await plans.draft(conn, indexer, project, "links", [], PlanBody(
            title="Links", goal="Shorten and redirect", out_of_scope=["auth"],
            tasks=[TaskSpec(title="Schema", spec="s"), TaskSpec(title="Routes", spec="s")],
            gates=[ProposedGate(name="imports", run="lint-imports", with_task=1)]))
        await conn.commit()
        return plan_id
    return run_db(go)


def status(run_db, doc_id):
    async def go(conn):
        return (await store.get_document(conn, doc_id))["status"]
    return run_db(go)


def test_the_human_approves_in_the_session_after_seeing_the_stored_plan(server, project, draft, run_db):
    result, asked = call(server, "plan_approve", {"project": project, "plan_id": draft}, "accept")
    assert result["status"] == "approved" and status(run_db, draft) == "approved"
    assert "Goal: Shorten and redirect" in asked[0] and "2. Routes" in asked[0]
    assert "imports (`lint-imports`, once task 1 merges)" in asked[0] and "/documents/" in asked[0]


@pytest.mark.parametrize("answer, human", [("decline", "declined"), ("cancel", "declined"), (None, "unavailable")])
def test_without_a_yes_from_the_human_nothing_is_approved(server, project, draft, run_db, answer, human):
    result, _ = call(server, "plan_approve", {"project": project, "plan_id": draft}, answer)
    assert result["status"] == "draft" and result["human"] == human and "approve_at" in result
    assert status(run_db, draft) == "draft"


def test_a_plan_in_a_wave_is_approved_with_its_wave(server, project, draft, run_db, indexer):
    async def go(conn):
        wave = await plans.draft_wave(conn, indexer, project, plans.WaveBody(
            title="MVP", goal="g", features=[plans.WaveFeature(plan_id=draft)]))
        await conn.commit()
        return wave
    wave = run_db(go)
    alone, _ = call(server, "plan_approve", {"project": project, "plan_id": draft}, "accept")
    assert "wave_approve" in alone["error"]
    result, asked = call(server, "wave_approve", {"project": project, "wave_id": wave}, "accept")
    assert result["status"] == "approved" and "1. Links: 2 tasks; adds gates imports" in asked[0]
    assert status(run_db, wave) == "approved" and status(run_db, draft) == "approved"
