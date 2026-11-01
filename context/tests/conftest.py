import asyncio
import os

# The tests embed with a word-hash fake, whose similarities say nothing; the real cutoff is measured
# for nomic-embed-text (store.MIN_SIMILARITY). Set before autonome_context is imported.
os.environ.setdefault("AUTONOME_SEARCH_MIN_SIMILARITY", "0")
import hashlib
import uuid

import psycopg
import pytest

from autonome_context import config
from autonome_context.chunking import STRATEGIES
from autonome_context.handoff import Decision, GateResult, Handoff, PlanItem
from autonome_context.migrate import migrate
from autonome_context.store import Indexer


class FakeEmbedder:
    """Deterministic bag-of-words vectors: shared words mean closer vectors. No Ollama needed."""

    model = "fake-bow"
    dims = 64

    async def embed(self, texts, purpose):
        out = []
        for t in texts:
            v = [0.0] * self.dims
            for word in t.lower().split():
                v[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dims] += 1.0
            out.append(v if any(v) else [1.0] + [0.0] * (self.dims - 1))
        return out


def make_handoff(project: str, **kw) -> Handoff:
    base = dict(
        project=project,
        session_id="s-1",
        feature="wallet",
        layers=["domain", "persistence"],
        summary="Implemented the append-only ledger for the wallet balance.",
        plan_adherence=[PlanItem(item="LedgerEntry entity", status="done")],
        gates=[GateResult(name="test", passed=False, retries=1, root_cause="missing tenant filter")],
        decisions=[Decision(title="Balance is derived", rationale="never stored, summed from entries")],
        friction=["no convention for money rounding"],
        adherence_score=0.8,
    )
    base.update(kw)
    return Handoff(**base)


@pytest.fixture(scope="session")
def db_url():
    """A separate autonome_test database, so fake embeddings never reach the dev index."""
    dev_url = config.load().database_url
    try:
        with psycopg.connect(dev_url, connect_timeout=2, autocommit=True) as conn:
            if not conn.execute("select 1 from pg_database where datname = 'autonome_test'").fetchone():
                conn.execute("create database autonome_test")
    except psycopg.OperationalError:
        pytest.skip("postgres not running (docker compose up -d)")
    url = dev_url.rsplit("/", 1)[0] + "/autonome_test"
    migrate(url)
    return url


@pytest.fixture
def project():
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def indexer():
    return Indexer(list(STRATEGIES.values()), FakeEmbedder())


@pytest.fixture
def run_db(db_url):
    """Runs an async function with a fresh connection: run_db(lambda conn: ...)."""

    def runner(fn):
        async def go():
            async with await psycopg.AsyncConnection.connect(db_url) as conn:
                return await fn(conn)

        return asyncio.run(go())

    return runner
