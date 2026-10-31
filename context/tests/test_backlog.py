import pytest

from autonome_context import backlog, imports, rules, store
from autonome_context.chunking import SectionV1

ROADMAP = """<script type="application/json" id="roadmap-data">
{"waves": [{"n": 1, "name": "Fundação local"}, {"n": 6, "name": "Infra + cutover"}],
 "cards": [{"id": "w1-ci", "wave": 1, "title": "CI por app", "status": "backlog"},
           {"id": "w1-skeleton", "wave": 1, "title": "Esqueleto do monorepo", "status": "next"},
           {"id": "w6-tunnel", "wave": 6, "title": "Cloudflare Tunnel", "status": "backlog"}]}
</script>"""


async def _seed(conn, indexer, project):
    await imports.import_backlog(conn, indexer, project, imports.parse_roadmap(ROADMAP))


def test_moving_an_item_between_waves_keeps_its_history_and_search_follows(run_db, project, indexer):
    async def go(conn):
        await _seed(conn, indexer, project)
        r = await backlog.update_item(conn, indexer, project, "w1-ci", by="agent:mcp", wave=6, status="next",
                                      note="after the NUC is up")
        waves = {w["wave"]: [i["id"] for i in w["items"]] for w in await backlog.list_backlog(conn, project)}
        history = await backlog.item_history(conn, project, "w1-ci")
        hits = await store.search(conn, "NUC", project, SectionV1(), indexer.embedder, doc_type="backlog", mode="fts")
        return r, waves, history, hits

    r, waves, history, hits = run_db(go)
    assert waves == {1: ["w1-skeleton"], 6: ["w6-tunnel", "w1-ci"]}
    assert r["item"] == {"id": "w1-ci", "title": "CI por app", "status": "next", "note": "after the NUC is up"}
    assert {(c["field"], str(c["old"]), str(c["new"]), c["by"]) for c in history} >= {
        ("status", "backlog", "next", "agent:mcp"), ("wave", "1", "6", "agent:mcp")}
    assert [h.section for h in hits] == ["w1-ci"]  # the item itself, in its new wave


def test_unknown_items_and_bad_input_are_refused_clearly(run_db, project, indexer):
    async def go(conn):
        await _seed(conn, indexer, project)
        errors = []
        for call in (lambda: backlog.update_item(conn, indexer, project, "w1-cii", by="t", status="done"),
                     lambda: backlog.update_item(conn, indexer, project, "w1-ci", by="t", status="finished"),
                     lambda: backlog.update_item(conn, indexer, project, "w1-ci", by="t", wave=9),
                     lambda: backlog.add_item(conn, indexer, project, 1, "w1-ci", "again", by="t")):
            with pytest.raises(rules.Forbidden) as e:
                await call()
            errors.append(str(e.value))
        return errors

    unknown, status, wave, dup = run_db(go)
    assert "no backlog item 'w1-cii'" in unknown and "did you mean w1-ci" in unknown
    assert "backlog, next, doing, done" in status and "no wave 9" in wave and "already exists" in dup


def test_items_are_added_and_removed_with_their_trace(run_db, project, indexer):
    async def go(conn):
        await _seed(conn, indexer, project)
        await backlog.add_item(conn, indexer, project, 1, "w1-audit", "Trilha de auditoria", by="human:ui", note="LGPD")
        await backlog.remove_item(conn, indexer, project, "w1-skeleton", by="human:ui", reason="done by hand")
        return await backlog.list_backlog(conn, project, 1), await backlog.item_history(conn, project, "w1-skeleton")

    [wave1], history = run_db(go)
    assert [i["id"] for i in wave1["items"]] == ["w1-ci", "w1-audit"]
    assert history[-1]["field"] == "removed" and history[-1]["old"]["title"] == "Esqueleto do monorepo"
    assert history[-1]["reason"] == "done by hand"
