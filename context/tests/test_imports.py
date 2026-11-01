from pathlib import Path

from autonome_context import imports, store

ADR = """# 0011 — Layering rules by convention, no ArchUnit

- **Status:** Superseded by 0013
- **Date:** 2026-10-29

## Context
One developer and a small codebase.

## Decision
Layering is checked by the reviewer.

## Consequences
Drift is caught late.
"""

ROADMAP = """<html><script type="application/json" id="roadmap-data">
{"updated": "2026-10-29", "waves": [{"n": 1, "name": "Fundação local"}, {"n": 2, "name": "checkout-api"}],
 "cards": [{"id": "w1-repo", "wave": 1, "title": "Criar repo", "status": "done"},
           {"id": "w1-skeleton", "wave": 1, "title": "Esqueleto do monorepo", "status": "next", "note": "pom agregador"},
           {"id": "w2-orders", "wave": 2, "title": "Consulta de pedidos", "status": "backlog", "prd": "docs/prd/pedidos.md"}]}
</script></html>"""


def test_an_adr_parses_into_a_decision_with_its_sections():
    d = imports.parse_adr(ADR, "0011-no-archunit.md")
    assert (d["adr"], d["status"], d["date"]) == (11, "superseded", "2026-10-29")
    assert d["title"] == "Layering rules by convention, no ArchUnit"
    assert d["decision"] == "Layering is checked by the reviewer." and d["consequences"] == "Drift is caught late."


def test_adrs_and_the_roadmap_import_once_and_update_on_a_second_run(run_db, project, indexer, tmp_path: Path):
    (tmp_path / "0011-no-archunit.md").write_text(ADR)

    async def go(conn):
        await imports.import_adrs(conn, indexer, project, tmp_path)
        (tmp_path / "0011-no-archunit.md").write_text(ADR.replace("Drift is caught late.", "Drift is caught late, by a human."))
        await imports.import_adrs(conn, indexer, project, tmp_path)
        waves = imports.parse_roadmap(ROADMAP)
        await imports.import_backlog(conn, indexer, project, waves)
        await imports.import_backlog(conn, indexer, project, waves)
        return (await store.list_documents(conn, project, type="decision"),
                await store.list_documents(conn, project, type="backlog"))

    decisions, backlog = run_db(go)
    assert len(decisions) == 1 and decisions[0]["status"] == "superseded"
    assert decisions[0]["body"]["consequences"] == "Drift is caught late, by a human."
    assert sorted((b["body"]["title"], b["status"], len(b["body"]["items"])) for b in backlog) == [
        ("Wave 1: Fundação local", "open", 2), ("Wave 2: checkout-api", "open", 1)]
