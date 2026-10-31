import asyncio
import hashlib
import uuid

import psycopg
import pytest

from autonome_context import config, store
from autonome_context.chunking import SectionV1
from autonome_context.handoff import Decision, GateResult, Handoff, PlanItem
from autonome_context.migrate import migrate
from autonome_context.redact import redact


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


def handoff(project: str, **kw) -> Handoff:
    base = dict(
        project=project,
        session_id="s-1",
        feature="wallet",
        layers=["domain", "persistence"],
        summary="Implemented the append-only ledger for the wallet balance.",
        plan_adherence=[PlanItem(item="LedgerEntry entity", status="done")],
        gates=[GateResult(name="test", passed=False, encores=1, root_cause="missing tenant filter")],
        decisions=[Decision(title="Balance is derived", rationale="never stored, summed from entries")],
        friction=["no convention for money rounding"],
        adherence_score=0.8,
    )
    base.update(kw)
    return Handoff(**base)


def test_redact_masks_identifiers_and_secrets():
    text = "cpf 123.456.789-09 and 12345678909, password=hunter22 key sk-abcdefghijklmnopqrstuvwx"
    out = redact(text)
    assert "123.456.789-09" not in out and "12345678909" not in out
    assert "hunter22" not in out and "sk-abcdef" not in out
    assert "password=[REDACTED:SECRET]" in out


def test_section_strategy_drops_empty_sections_and_prefixes_context():
    chunks = SectionV1().chunk(handoff("p", deviations=[]))
    sections = [c.section for c in chunks]
    assert "deviations" not in sections
    assert sections[0] == "summary"
    assert chunks[0].text.startswith("feature: wallet | layers: domain, persistence")


@pytest.fixture(scope="module")
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


def test_put_and_search_roundtrip(db_url):
    async def run():
        project = f"test-{uuid.uuid4().hex[:8]}"
        emb, strat = FakeEmbedder(), SectionV1()
        async with await psycopg.AsyncConnection.connect(db_url) as conn:
            await store.put_handoff(conn, handoff(project), "raw cpf 123.456.789-09", strat, emb)
            await store.put_handoff(
                conn,
                handoff(project, feature="signup", layers=["ui"], summary="Built the signup form.",
                        decisions=[], gates=[], friction=[]),
                None, strat, emb,
            )
            hits = await store.search(conn, "tenant filter", project, strat, emb)
            filtered = await store.search(conn, "form", project, strat, emb, layer="ui")
            raw = await (await conn.execute(
                "select content_gz from raw_transcript rt join document d on d.id = rt.document_id"
                " join project p on p.id = d.project_id where p.slug = %s", (project,))).fetchone()
        return hits, filtered, raw

    hits, filtered, raw = asyncio.run(run())
    assert hits[0].section == "gates" and hits[0].feature == "wallet"
    assert {h.feature for h in filtered} == {"signup"}
    import gzip
    assert b"123.456.789-09" not in gzip.decompress(raw[0])
