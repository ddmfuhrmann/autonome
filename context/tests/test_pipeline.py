import gzip

from autonome_context import evaluation, store
from autonome_context.chunking import STRATEGIES, Doc, SectionV1, WholeV1
from autonome_context.redact import redact

from conftest import make_handoff


def test_redact_masks_identifiers_and_secrets():
    text = "cpf 123.456.789-09 and 12345678909, password=hunter22 key sk-abcdefghijklmnopqrstuvwx"
    out = redact(text)
    assert "123.456.789-09" not in out and "12345678909" not in out
    assert "hunter22" not in out and "sk-abcdef" not in out
    assert "password=[REDACTED:SECRET]" in out


def test_section_strategy_drops_empty_sections_and_prefixes_context():
    h = make_handoff("p", deviations=[])
    chunks = SectionV1().chunk(Doc("handoff", h.model_dump(), h.feature, h.layers))
    sections = [c.section for c in chunks]
    assert "deviations" not in sections
    assert sections[0] == "summary"
    assert chunks[0].text.startswith("handoff | feature: wallet | layers: domain, persistence")


def test_generic_documents_chunk_by_top_level_field():
    doc = Doc("plan", {"title": "Signup", "goal": "Self-service signup", "tasks": [{"title": "form"}]}, None, [])
    assert [c.section for c in SectionV1().chunk(doc)] == ["title", "goal", "tasks"]
    assert len(WholeV1().chunk(doc)) == 1


def test_metrics():
    m = evaluation.score([5, 3, 9], {3}, k=3)
    assert m.recall == 1 and m.mrr == 0.5 and round(m.ndcg, 3) == 0.631
    assert evaluation.score([1, 2], {7}, k=2).recall == 0


def test_put_and_search_roundtrip(run_db, project, indexer):
    async def go(conn):
        await store.put_handoff(conn, indexer, make_handoff(project), "raw cpf 123.456.789-09")
        await store.put_handoff(conn, indexer, make_handoff(
            project, feature="signup", layers=["ui"], summary="Built the signup form.",
            decisions=[], gates=[], friction=[]), None)
        hits = await store.search(conn, "tenant filter", project, SectionV1(), indexer.embedder)
        filtered = await store.search(conn, "form", project, SectionV1(), indexer.embedder, layer="ui")
        fts = await store.search(conn, "rounding", project, SectionV1(), indexer.embedder, mode="fts")
        decisions = await store.list_documents(conn, project, type="decision")
        raw = await (await conn.execute(
            "select content_gz from raw_transcript rt join document d on d.id = rt.document_id"
            " join project p on p.id = d.project_id where p.slug = %s", (project,))).fetchone()
        return hits, filtered, fts, decisions, raw

    hits, filtered, fts, decisions, raw = run_db(go)
    assert hits[0].type == "handoff" and hits[0].section == "gates"
    assert len({h.document_id for h in hits}) == len(hits)  # one hit per document
    assert {h.feature for h in filtered} == {"signup"}
    assert [h.section for h in fts] == ["friction"] and fts[0].ranks == {"fts": 1}
    assert decisions[0]["status"] == "accepted" and decisions[0]["body"]["title"] == "Balance is derived"  # within the plan
    assert b"123.456.789-09" not in gzip.decompress(raw[0])


def test_every_strategy_indexes_and_new_versions_replace_old_in_search(run_db, project, indexer):
    async def go(conn):
        doc_id = await store.create_document(conn, indexer, project=project, type="convention",
                                             body={"title": "Money", "rule": "round half even"})
        await store.add_version(conn, indexer, doc_id, {"title": "Money", "rule": "round half up"})
        per_strategy = await (await conn.execute(
            "select s.name, count(*) from chunk c join strategy s on s.id = c.strategy_id"
            " join document_version dv on dv.id = c.document_version_id where dv.document_id = %s"
            " group by s.name", (doc_id,))).fetchall()
        hits = await store.search(conn, "even", project, SectionV1(), indexer.embedder, mode="fts")
        doc = await store.get_document(conn, doc_id)
        return dict(per_strategy), hits, doc

    per_strategy, hits, doc = run_db(go)
    assert set(per_strategy) == set(STRATEGIES)
    assert hits == []  # "even" only exists in version 1
    assert doc["version"] == 2 and doc["body"]["rule"] == "round half up"


def test_eval_run_records_metrics(run_db, project, indexer):
    async def go(conn):
        doc_id = await store.put_handoff(conn, indexer, make_handoff(project), None)
        await evaluation.add_question(conn, project, "missing tenant filter in tests", [doc_id], "manual")
        result = await evaluation.run(conn, project, SectionV1(), indexer.embedder, "hybrid", 5)
        return result, await evaluation.runs(conn, project)

    result, runs = run_db(go)
    assert result["n"] == 1 and result["recall"] == 1.0
    assert runs[0]["strategy"] == "section-v1"
