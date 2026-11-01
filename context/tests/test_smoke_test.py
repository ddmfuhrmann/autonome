from autonome_context import plans, rules, store
from autonome_context.plans import Fix, SoundcheckResult

from test_waves import body, play

SMOKE_TEST = "smoke_test: {up: 'true', down: 'true', url: 'http://127.0.0.1:8000', max_fixes: 1}\n"


async def with_smoke_test(conn, indexer, project):
    async with conn.transaction():
        await store.save_singleton(conn, indexer, project, "gate_config", {"yaml": SMOKE_TEST})
    plan_id = await plans.draft(conn, indexer, project, "links", ["api"], body("Links"))
    [task] = await plans.approve(conn, indexer, plan_id)
    await play(conn, task)
    await store.finish_plan_if_complete(conn, plan_id)
    return plan_id


def failed(*titles):
    return SoundcheckResult(passed=False, report="redirect returns 500",
                            fixes=[Fix(title=t, spec="GET /abc returns 500; it should redirect") for t in titles])


def test_a_finished_feature_is_done_only_after_its_smoke_test_passes(run_db, project, indexer):
    async def go(conn):
        plan_id = await with_smoke_test(conn, indexer, project)
        status_before = (await store.get_document(conn, plan_id))["status"]
        due = await plans.soundcheck_due(conn, project)
        outcome = await plans.record_soundcheck(conn, indexer, plan_id, SoundcheckResult(passed=True, report="ok"))
        return plan_id, status_before, due, outcome, (await store.get_document(conn, plan_id))["status"], \
            await plans.soundcheck_due(conn, project)

    plan_id, before, due, outcome, after, due_after = run_db(go)
    assert before == "approved" and due["id"] == plan_id
    assert outcome == "done" and after == "done" and due_after is None


def test_a_failed_smoke_test_queues_fixes_then_blocks_when_rounds_run_out(run_db, project, indexer):
    async def go(conn):
        plan_id = await with_smoke_test(conn, indexer, project)
        first = await plans.record_soundcheck(conn, indexer, plan_id, failed("Fix redirect"))
        fix = await store.next_task(conn, project)
        due_while_fixing = await plans.soundcheck_due(conn, project)
        await play(conn, fix["id"])
        second = await plans.record_soundcheck(conn, indexer, plan_id, failed("Fix it again"))
        blocked = (await store.get_document(conn, plan_id))["status"]
        await plans.retry_soundcheck(conn, indexer, plan_id)
        retried_due = await plans.soundcheck_due(conn, project)
        third = await plans.record_soundcheck(conn, indexer, plan_id, failed("Third try"))
        return first, fix, due_while_fixing, second, blocked, retried_due, third

    first, fix, due_while_fixing, second, blocked, retried_due, third = run_db(go)
    assert first == "fixing" and fix["body"]["title"] == "Fix redirect" and fix["body"]["source"] == "soundcheck"
    assert due_while_fixing is None
    assert second == "blocked" and blocked == "blocked"
    assert retried_due is not None and third == "fixing"  # a human retry gives fresh fix rounds


def test_a_failure_with_nothing_to_fix_blocks_and_a_human_can_accept_it(run_db, project, indexer):
    async def go(conn):
        plan_id = await with_smoke_test(conn, indexer, project)
        outcome = await plans.record_soundcheck(conn, indexer, plan_id, failed())
        try:
            await rules.transition(conn, plan_id, "done", "runner")
            runner_could = True
        except rules.Forbidden:
            runner_could = False
        await plans.accept_as_is(conn, plan_id)
        return outcome, runner_could, (await store.get_document(conn, plan_id))["status"]

    outcome, runner_could, status = run_db(go)
    assert outcome == "blocked" and not runner_could and status == "done"
