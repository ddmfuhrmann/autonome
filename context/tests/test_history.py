from autonome_context import history, plans, rules, store
from autonome_context.handoff import Decision, PlanItem
from autonome_context.plans import PlanBody, Preflight, TaskSpec

from conftest import make_handoff


def test_review_rounds_are_read_from_reviews_and_old_runs_from_review():
    def f(severity, issue, outcome=None):
        return {"kind": "missing", "severity": severity, "issue": issue, **({"outcome": outcome} if outcome else {})}

    last = {"verdict": "pass", "findings": [f("medium", "weak test", "merged")]}
    two_rounds = {"reviews": [{"verdict": "fail", "findings": [f("high", "no test", "resolved")]}, last], "review": last}
    old = {"review": last}
    assert plans.loose_ends(two_rounds) == plans.loose_ends(old) == ["review, medium missing: weak test"]
    assert plans.loose_ends(None) == []


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
        await rules.transition(conn, task, "running", "runner")
        await rules.transition(conn, task, "blocked", "runner")
        doc = await store.get_document(conn, task)
        await store.add_version(conn, None, task, {**doc["body"], "runs": [{"outcome": "blocked", "reason": "ledger updated in place",
            "review": {"verdict": "fail", "findings": [{"kind": "diverges", "severity": "high", "issue": "updates a ledger row"}]}}]})
        before = await history.build(conn, project, "debit the wallet", indexer.embedder, feature="wallet")
        # A run with two rounds: a finding fixed in round 2 (resolved) still counts as a block it was.
        rounds = [{"verdict": "fail", "findings": [{"kind": "missing", "severity": "high", "issue": "no zero check"}]},
                  {"verdict": "pass", "findings": []}]
        doc = await store.get_document(conn, task)
        await store.add_version(conn, None, task, {**doc["body"], "runs": [
            *doc["body"]["runs"], {"outcome": "merged", "reviews": rounds, "review": rounds[-1]}]})
        after = await history.build(conn, project, "debit the wallet", indexer.embedder, feature="wallet")
        assert [b["issue"] for b in after["review_blocks"]] == ["updates a ledger row", "no zero check"]
        assert after["review_blocks"][:1] == before["review_blocks"]
        await store.add_version(conn, None, task, doc["body"])
        await store.enqueue_task(conn, indexer, project, title="Fix: Debit", spec="reject negative amounts",
                                 feature="wallet", source="after_merge:9")
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
    assert [(f["source"], f["title"]) for f in h["fixes"]] == [("after_merge", "Fix: Debit")]
