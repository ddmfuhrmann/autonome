import psycopg_pool
import pytest
from starlette.testclient import TestClient

from autonome_context import services, store
from autonome_context.server import build_app

from conftest import add_feature, make_handoff


@pytest.fixture
def client(db_url, indexer, monkeypatch):
    async def get():
        pool = psycopg_pool.AsyncConnectionPool(db_url, open=False, min_size=1, max_size=2)
        await pool.open()
        return pool, indexer

    monkeypatch.setattr(services, "get", get)
    with TestClient(build_app(), base_url="http://127.0.0.1:8765") as c:
        yield c


def test_post_handoff_with_task_id_stores_the_tasks_feature(client, run_db, project, indexer):
    async def go(conn):
        await add_feature(conn, project, "wallet", "ledger of balances")
        await add_feature(conn, project, "signup", "register an account with a form")
        task = await _task(conn, indexer, project, "signup")
        await conn.commit()
        return task

    task = run_db(go)
    handoff = make_handoff(project, feature="wallet")
    r = client.post("/api/handoffs", json={"handoff": handoff.model_dump(mode="json"), "task_id": task})
    assert r.status_code == 201
    stored = run_db(lambda conn: store.get_document(conn, r.json()["document_id"]))
    assert stored["feature"] == "signup"


def test_features_api_lists_description_and_paths(client, run_db, project, indexer):
    async def go(conn):
        await store.resolve_feature(conn, indexer.embedder, project, "signup", "register an account", ["src/signup"])
        await conn.commit()

    run_db(go)
    [f] = client.get(f"/api/projects/{project}/features").json()
    assert f["slug"] == "signup" and f["description"] == "register an account" and f["paths"] == ["src/signup"]


async def _task(conn, indexer, project, feature):
    return await store.create_document(
        conn, indexer, project=project, type="task", status="done", feature=feature, layers=[],
        body={"title": "t", "what": "w"},
    )


def test_handoff_with_task_gets_the_tasks_feature(run_db, project, indexer):
    async def go(conn):
        await add_feature(conn, project, "wallet", "ledger of balances")
        await add_feature(conn, project, "signup", "register an account with a form")
        task = await _task(conn, indexer, project, "signup")
        doc_id = await store.put_handoff(conn, indexer, make_handoff(project, feature="wallet"), None, task)
        handoff = await store.get_document(conn, doc_id)
        decisions = await store.list_documents(conn, project, type="decision")
        return handoff, decisions

    handoff, decisions = run_db(go)
    assert handoff["feature"] == "signup"
    assert [d["feature"] for d in decisions] == ["signup"]


def test_handoff_with_a_missing_task_fails_without_creating_a_feature(run_db, project, indexer):
    async def go(conn):
        with pytest.raises(ValueError):
            await store.put_handoff(
                conn, indexer,
                make_handoff(project, feature="brand-new", feature_description="a brand new capability"), None, 999999,
            )
        return await store.catalog(conn, project)

    assert run_db(go) == []


def test_new_feature_similar_to_an_existing_one_is_stored_under_it(run_db, project, indexer):
    async def go(conn):
        await add_feature(conn, project, "wallet", "ledger of wallet balances and entries")
        doc_id = await store.put_handoff(conn, indexer, make_handoff(
            project, feature="wallet-ledger", feature_description="ledger of wallet balances and entries"), None)
        return await store.get_document(conn, doc_id), await store.catalog(conn, project)

    handoff, catalog = run_db(go)
    assert handoff["feature"] == "wallet"
    assert [f["slug"] for f in catalog] == ["wallet"]


def test_new_feature_with_a_description_enters_the_catalog(run_db, project, indexer):
    async def go(conn):
        doc_id = await store.put_handoff(conn, indexer, make_handoff(
            project, feature="signup", feature_description="register an account with a form",
            feature_paths=["src/signup"]), None)
        return await store.get_document(conn, doc_id), await store.catalog(conn, project)

    handoff, catalog = run_db(go)
    assert handoff["feature"] == "signup"
    assert catalog[0]["paths"] == ["src/signup"]


def test_new_feature_without_description_is_stored_without_feature(run_db, project, indexer):
    async def go(conn):
        doc_id = await store.put_handoff(conn, indexer, make_handoff(project, feature="invented"), "raw")
        doc = await store.get_document(conn, doc_id)
        decisions = await store.list_documents(conn, project, type="decision")
        return doc, decisions, await store.catalog(conn, project)

    doc, decisions, catalog = run_db(go)
    assert doc["feature"] is None and doc["type"] == "handoff"
    assert [d["feature"] for d in decisions] == [None]
    assert catalog == []


def test_failing_embedder_does_not_stop_the_handoff(run_db, project, indexer):
    class Broken:
        model = "broken"
        dims = 64

        async def embed(self, texts, purpose):
            raise ConnectionError("ollama is down")

    async def go(conn):
        indexer.embedder = Broken()
        doc_id = await store.put_handoff(conn, indexer, make_handoff(
            project, feature="signup", feature_description="register an account with a form"), None)
        return await store.get_document(conn, doc_id), await store.catalog(conn, project)

    doc, catalog = run_db(go)
    assert doc["feature"] is None
    assert catalog == []
