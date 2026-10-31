from autonome_context import history, plans, rules, store
from autonome_context.handoff import Decision, PlanItem
from autonome_context.plans import PlanBody, Rehearsal, TaskSpec

from conftest import make_handoff


def test_the_history_of_a_subject_gathers_what_went_off_plan(run_db, project, indexer):
    async def go(conn):
        await store.put_handoff(conn, indexer, make_handoff(
            project, feature="wallet", summary="Debit with the ledger",
            plan_adherence=[PlanItem(item="Reject zero amounts", status="skipped", note="ran out of budget")],
            deviations=["Balance cached in a column"],
            decisions=[Decision(title="Cached the balance", rationale="faster reads", deviation=True)],
            friction=["no convention for rounding"]), None)
        plan_id = await plans.draft(conn, indexer, project, "wallet", [], PlanBody(
            title="Wallet", goal="g", tasks=[TaskSpec(title="Debit", spec="s")]))
        [task] = await plans.approve(conn, indexer, plan_id)
        await rules.transition(conn, task, "playing", "runner")
        await rules.transition(conn, task, "blocked", "runner")
        doc = await store.get_document(conn, task)
        await store.add_version(conn, None, task, {**doc["body"], "runs": [{"outcome": "blocked", "reason": "ledger updated in place",
            "review": {"verdict": "fail", "findings": [{"kind": "diverges", "severity": "high", "issue": "updates a ledger row"}]}}]})
        await store.enqueue_task(conn, indexer, project, title="Fix: Debit", spec="reject negative amounts",
                                 feature="wallet", source="playback:9")
        await store.put_handoff(conn, indexer, make_handoff(project, feature="signup", summary="Signup form",
                                                            deviations=["unrelated"]), None)
        return await history.build(conn, project, "debit the wallet", indexer.embedder, feature="wallet")

    h = run_db(go)
    assert h["features"][0] == "wallet" and not h["empty"]
    assert [(c["item"], c["status"]) for c in h["plan_changes"]] == [("Reject zero amounts", "skipped")]
    assert [d["text"] for d in h["deviations"] if d["feature"] == "wallet"] == ["Balance cached in a column"]
    assert [(d["title"], d["status"]) for d in h["deviation_decisions"]] == [("Cached the balance", "proposed")]
    assert [b["issue"] for b in h["review_blocks"]] == ["updates a ledger row"]
    assert [b["reason"] for b in h["blocked"]] == ["ledger updated in place"]
    assert [(f["source"], f["title"]) for f in h["fixes"]] == [("playback", "Fix: Debit")]
