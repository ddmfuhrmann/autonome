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
