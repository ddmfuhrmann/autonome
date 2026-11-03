from autonome_context import insights, store


def test_spend_of_the_roles_outside_the_runs_shows_in_insights(run_db, project, indexer):
    async def go(conn):
        await store.record_spend(conn, project, "handoff", 0.17, tokens=40_000)
        await store.record_spend(conn, project, "handoff", 0.13)
        await store.record_spend(conn, project, "docs", 0.27, feature="wallet")
        await conn.commit()
        return await insights.build(conn, project, indexer.embedder)

    other = run_db(go)["spent"]["other_roles"]
    assert other == {"docs": {"sessions": 1, "cost_usd": 0.27}, "handoff": {"sessions": 2, "cost_usd": 0.3}}
    assert list(other) == ["handoff", "docs"]  # costliest first
