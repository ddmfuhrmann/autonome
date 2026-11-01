from autonome_context import plans, store
from autonome_context.chunking import STRATEGIES, Chunk, SectionV1
from autonome_context.plans import PlanBody, TaskSpec
from autonome_context.store import Indexer

from conftest import FakeEmbedder


class ContextLimitedEmbedder(FakeEmbedder):
    """Fails like Ollama does when a text exceeds the model's context."""

    max_chars = 1000

    async def embed(self, texts, purpose):
        if any(len(t) > self.max_chars for t in texts):
            raise RuntimeError("the input length exceeds the context length")
        return await super().embed(texts, purpose)


def test_fit_splits_long_chunks_at_line_breaks_and_keeps_sections():
    text = "".join(f"line {i} " + "x" * 90 + "\n" for i in range(30)) + "y" * 250
    parts = store.fit([Chunk(0, "tasks", text), Chunk(1, "goal", "short")], 1000)
    assert all(len(p.text) <= 1000 for p in parts)
    assert [p.ord for p in parts] == list(range(len(parts)))
    assert {p.section for p in parts[:-1]} == {"tasks"} and parts[-1].section == "goal"
    assert "".join(p.text for p in parts[:-1]) == text


def test_a_plan_longer_than_the_model_context_indexes_and_stays_searchable(run_db, project):
    indexer = Indexer(list(STRATEGIES.values()), ContextLimitedEmbedder())
    tasks = [TaskSpec(title=f"Task {i}", spec=f"step {i}: " + "do the thing carefully " * 40) for i in range(30)]
    tasks[-1] = TaskSpec(title="Last", spec="wire the zebracounter into the redirect handler")

    async def go(conn):
        plan_id = await plans.draft(conn, indexer, project, "links", [], PlanBody(title="Big", goal="g", tasks=tasks))
        return plan_id, await store.search(conn, "zebracounter", project, SectionV1(), indexer.embedder, mode="fts")

    plan_id, hits = run_db(go)
    assert [h.document_id for h in hits] == [plan_id]


class BrokenEmbedder(FakeEmbedder):
    async def embed(self, texts, purpose):
        raise RuntimeError("ollama is down")


def test_a_failed_embedding_never_fails_the_write_and_reindex_picks_it_up(run_db, project, db_url, indexer, monkeypatch):
    import psycopg_pool
    from starlette.testclient import TestClient

    from autonome_context import services
    from autonome_context.server import build_app

    async def setup(conn):
        plan_id = await plans.draft(conn, indexer, project, "links", [], PlanBody(
            title="Links", goal="g", tasks=[TaskSpec(title="t", spec="s")]))
        await plans.approve(conn, indexer, plan_id)
        return plan_id

    plan_id = run_db(setup)
    broken = Indexer(list(STRATEGIES.values()), BrokenEmbedder())

    async def get():
        pool = psycopg_pool.AsyncConnectionPool(db_url, open=False, min_size=1, max_size=2)
        await pool.open()
        return pool, broken

    monkeypatch.setattr(services, "get", get)
    with TestClient(build_app(), base_url="http://127.0.0.1:8765") as client:
        r = client.post(f"/api/plans/{plan_id}/rehearsal", json={"verdict": "ready", "notes": "ok"})

    async def after(conn):
        doc = await store.get_document(conn, plan_id)
        indexed_before = await (await conn.execute(
            "select count(*) from chunk c join document_version dv on dv.id = c.document_version_id"
            " where dv.document_id = %s and dv.version = %s", (plan_id, doc["version"]))).fetchone()
        reindexed = await store.reindex(conn, SectionV1(), indexer.embedder)
        return doc, indexed_before[0], reindexed

    doc, indexed_before, reindexed = run_db(after)
    assert r.status_code == 200 and r.json() == {"plan_status": "approved"}
    assert doc["body"]["preflight"]["notes"] == "ok" and indexed_before == 0
    assert reindexed >= 1
