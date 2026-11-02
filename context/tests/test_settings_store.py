import json

import psycopg_pool
import pytest
from starlette.testclient import TestClient

from autonome_context import plans, rules, services, store
from autonome_context.plans import PlanBody, ProposedGate, TaskSpec
from autonome_context.server import build_app

from conftest import add_feature


@pytest.fixture
def client(db_url, indexer, monkeypatch):
    async def get():
        pool = psycopg_pool.AsyncConnectionPool(db_url, open=False, min_size=1, max_size=2)
        await pool.open()
        return pool, indexer

    monkeypatch.setattr(services, "get", get)
    with TestClient(build_app(), base_url="http://127.0.0.1:8765") as c:
        yield c


def plan_body(**kw) -> PlanBody:
    return PlanBody(title="Web", goal="Web goal",
                    tasks=[TaskSpec(title=f"Web {i}", spec=f"do {i}") for i in (1, 2)], **kw)


def test_approving_a_plan_and_merging_its_task_record_where_each_gate_came_from(client, run_db, project, indexer):
    async def setup(conn):
        await add_feature(conn, project, "web")
        async with conn.transaction():
            await store.save_singleton(conn, indexer, project, "gate_config",
                                       {"yaml": "gates:\n  - {name: test, run: pytest}\n"})
        plan_id = await plans.draft(conn, indexer, project, "web", [], plan_body(gates=[
            ProposedGate(name="lint", run="ruff check ."),
            ProposedGate(name="e2e", run="pnpm test", with_task=2)]))
        first, second = await plans.approve(conn, indexer, plan_id)
        await rules.transition(conn, second, "running", "runner")
        await conn.commit()
        return plan_id, second

    plan_id, task_id = run_db(setup)
    assert client.post(f"/api/tasks/{task_id}/transition", json={"to": "done"}).status_code == 200

    versions = run_db(lambda c: store.gate_config_versions(c, project))
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert versions[0]["body"]["source"] == {"plan_id": plan_id, "task_id": task_id}
    assert versions[1]["body"]["source"] == {"plan_id": plan_id, "task_id": None}
    assert "source" not in versions[2]["body"]


def test_versions_come_newest_first_only_from_the_asked_project_and_old_ones_stay_readable(run_db, project, indexer):
    other = project + "-other"

    async def go(conn):
        async with conn.transaction():
            await store.save_singleton(conn, indexer, project, "gate_config", {"yaml": "gates: []\n"})
            await store.save_singleton(conn, indexer, project, "gate_config",
                                       {"yaml": "gates: []\n", "source": "human", "note": "by hand"})
            await store.save_singleton(conn, indexer, other, "gate_config", {"yaml": "gates: []\n"})
        return await store.gate_config_versions(conn, project), await store.gate_config_versions(conn, other)

    mine, theirs = run_db(go)
    assert [v["version"] for v in mine] == [2, 1] and len(theirs) == 1
    assert mine[0]["body"]["source"] == "human" and mine[0]["body"]["note"] == "by hand"
    assert mine[1]["body"] == {"yaml": "gates: []\n"} and mine[1]["created_at"]
    assert json.dumps(mine[0]["body"])
