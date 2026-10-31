import psycopg_pool
import pytest
from starlette.testclient import TestClient

from autonome_context import plans, rules, services, store
from autonome_context.plans import PlanBody, TaskSpec
from autonome_context.server import build_app

FETCH = {"X-Requested-With": "fetch"}


@pytest.fixture
def client(db_url, indexer, monkeypatch):
    async def get():
        pool = psycopg_pool.AsyncConnectionPool(db_url, open=False, min_size=1, max_size=2)
        await pool.open()
        return pool, indexer

    monkeypatch.setattr(services, "get", get)
    with TestClient(build_app(), base_url="http://127.0.0.1:8765") as c:
        yield c


@pytest.fixture
def blocked_task(run_db, project, indexer):
    async def go(conn):
        plan_id = await plans.draft(conn, indexer, project, "links", [], PlanBody(
            title="Links", goal="g", tasks=[TaskSpec(title="t", spec="s")]))
        [task] = await plans.approve(conn, indexer, plan_id)
        await rules.transition(conn, task, "playing", "runner")
        await rules.transition(conn, task, "blocked", "runner")
        await conn.commit()
        return task

    return run_db(go)


def status(run_db, doc_id):
    async def go(conn):
        return (await store.get_document(conn, doc_id))["status"]
    return run_db(go)


def test_a_retry_answers_with_the_confirmed_status(client, project, blocked_task, run_db):
    r = client.post(f"/ui/p/{project}/documents/{blocked_task}/transition", data={"to": "pending"}, headers=FETCH)
    assert r.status_code == 200 and r.json()["status"] == "pending" and "is now pending" in r.json()["notice"]
    assert status(run_db, blocked_task) == "pending"
    page = client.get(f"/ui/p/{project}/documents/{blocked_task}", params={"done": r.json()["notice"]})
    assert "notice--ok" in page.text


def test_a_refused_or_failed_transition_is_reported_and_changes_nothing(client, project, blocked_task, run_db, monkeypatch):
    r = client.post(f"/ui/p/{project}/documents/{blocked_task}/transition", data={"to": "done"}, headers=FETCH)
    assert r.status_code == 409 and "human may not move" in r.json()["error"]

    async def down(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(rules, "transition", down)
    r = client.post(f"/ui/p/{project}/documents/{blocked_task}/transition", data={"to": "pending"}, headers=FETCH)
    assert r.status_code == 503 and "could not save it" in r.json()["error"]
    assert status(run_db, blocked_task) == "blocked"
