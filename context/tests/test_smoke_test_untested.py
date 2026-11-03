import psycopg_pool
import pytest
from starlette.testclient import TestClient

from autonome_context import plans, services, store
from autonome_context.plans import SmokeTestResult
from autonome_context.server import build_app

from test_smoke_test import with_smoke_test

NOTES = [{"criterion": "An e-mail is sent", "why": "needs a real SMTP server"},
         {"criterion": "The nightly job runs", "why": "no way to trigger it"}]


@pytest.fixture
def client(db_url, indexer, monkeypatch):
    async def get():
        pool = psycopg_pool.AsyncConnectionPool(db_url, open=False, min_size=1, max_size=2)
        await pool.open()
        return pool, indexer

    monkeypatch.setattr(services, "get", get)
    with TestClient(build_app(), base_url="http://127.0.0.1:8765") as c:
        yield c


def test_untested_is_accepted_stored_and_shown_on_the_plan_page(client, run_db, project, indexer):
    async def make(conn):
        plan_id = await with_smoke_test(conn, indexer, project)
        await conn.commit()
        return plan_id

    plan_id = run_db(make)
    r = client.post(f"/api/plans/{plan_id}/smoke-test", json={"passed": True, "report": "ok", "untested": NOTES})
    assert r.status_code == 200 and r.json() == {"outcome": "done"}

    async def stored(conn):
        return (await store.get_document(conn, plan_id))["body"]["smoke_tests"][-1]["untested"]

    assert run_db(stored) == NOTES
    page = client.get(f"/ui/p/{project}/documents/{plan_id}").text
    assert "Untested" in page and "An e-mail is sent" in page and "needs a real SMTP server" in page


def test_too_many_or_too_long_untested_notes_answer_422(client, run_db, project, indexer):
    async def make(conn):
        plan_id = await with_smoke_test(conn, indexer, project)
        await conn.commit()
        return plan_id

    plan_id = run_db(make)
    url = f"/api/plans/{plan_id}/smoke-test"
    ok = {"criterion": "c", "why": "w"}

    def post(untested):
        return client.post(url, json={"passed": True, "report": "ok", "untested": untested}).status_code

    assert post([ok] * 31) == 422
    assert post([{"criterion": "c" * 501, "why": "w"}]) == 422
    assert post([{"criterion": "c", "why": "w" * 501}]) == 422
    assert post([{"criterion": "c" * 500, "why": "w" * 500}] * 30) == 200  # the limits themselves are fine


def test_untested_defaults_to_empty():
    assert SmokeTestResult(passed=True, report="ok").untested == []
    assert plans.SmokeTestResult.model_validate({"passed": True, "report": "ok"}).untested == []
