"""The init's settings proposal: kept on the project, never read as the gate config."""

import asyncio

import psycopg_pool
import pytest

from autonome_context import api, server, services, settings_proposal, store

VALID = "gates:\n  - name: tests\n    run: uv run pytest\n"
OTHER = "gates:\n  - name: lint\n    run: uv run ruff check .\n"


@pytest.fixture
def tool(db_url, indexer, monkeypatch):
    """Calls the MCP tool function on one event loop with its own pool."""
    def call(**args):
        async def go():
            pool = psycopg_pool.AsyncConnectionPool(db_url, open=False, min_size=1, max_size=2)
            await pool.open()

            async def get():
                return pool, indexer

            monkeypatch.setattr(services, "get", get)
            try:
                return await server.settings_propose(**args)
            finally:
                await pool.close()

        return asyncio.run(go())

    return call


def test_valid_yaml_is_stored_and_the_gate_config_stays_empty(tool, run_db, project):
    result = tool(project=project, yaml=VALID, note="from the Makefile")

    assert result == {"ok": True, "review_at": f"{services.settings.public_url}/ui/p/{project}/settings/proposal"}

    async def go(conn):
        return (await settings_proposal.get(conn, project), await api.gate_config_body(conn, project),
                await store.latest_of_type(conn, project, "gate_config"))

    proposal, body, latest = run_db(go)
    assert proposal["yaml"] == VALID and proposal["note"] == "from the Makefile" and proposal["at"]
    assert body["version"] is None and body["config"]["gates"] == []
    assert latest is None


@pytest.mark.parametrize("text", ["gates: [", "gates:\n  - name: tests\n    run: ''\n", "bogus_key: 1\ngates: 3"])
def test_invalid_yaml_or_empty_run_returns_problems_and_stores_nothing(tool, run_db, project, text):
    result = tool(project=project, yaml=text)

    assert "error" in result and result["problems"]
    assert run_db(lambda conn: settings_proposal.get(conn, project)) is None


def test_second_proposal_replaces_the_first(tool, run_db, project):
    tool(project=project, yaml=VALID, note="first")
    tool(project=project, yaml=OTHER, note="second")

    proposal = run_db(lambda conn: settings_proposal.get(conn, project))
    assert proposal["yaml"] == OTHER and proposal["note"] == "second"


def test_missing_project_is_created(tool, run_db, project):
    assert run_db(lambda conn: store.project_exists(conn, project)) is False
    tool(project=project, yaml=VALID)
    assert run_db(lambda conn: store.project_exists(conn, project)) is True


@pytest.mark.parametrize("slug", ["has space", "tab\there", "nul\x00", ""])
def test_bad_slug_is_refused(tool, run_db, slug):
    result = tool(project=slug, yaml=VALID)

    assert "error" in result
    assert run_db(lambda conn: store.project_exists(conn, slug.replace("\x00", ""))) is False


@pytest.mark.parametrize("note", ["x" * 501, "bad\x00note"])
def test_bad_note_is_refused(tool, run_db, project, note):
    result = tool(project=project, yaml=VALID, note=note)

    assert "note" in result["problems"]
    assert run_db(lambda conn: store.project_exists(conn, project)) is False


def test_oversized_yaml_is_refused(tool, project):
    result = tool(project=project, yaml=VALID + "# " + "x" * 70000)

    assert "yaml" in result["problems"]


def test_clear_removes_the_proposal(tool, run_db, project):
    tool(project=project, yaml=VALID)

    async def go(conn):
        await settings_proposal.clear(conn, project)
        return await settings_proposal.get(conn, project)

    assert run_db(go) is None
