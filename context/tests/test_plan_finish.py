from autonome_context import plans, rules, store
from autonome_context.plans import WaveBody, WaveFeature

from conftest import add_feature
from test_smoke_test import SMOKE_TEST
from test_waves import body
from test_web import FETCH, client, status  # noqa: F401  (the client fixture)


def abandon(client, project, task):
    return client.post(f"/ui/p/{project}/documents/{task}/transition", data={"to": "abandoned"}, headers=FETCH)


def approved_plan(run_db, indexer, project, tasks=2, in_wave=False, smoke_test=False):
    async def go(conn):
        if smoke_test:
            async with conn.transaction():
                await store.save_singleton(conn, indexer, project, "gate_config", {"yaml": SMOKE_TEST})
        await add_feature(conn, project, "links")
        plan_id = await plans.draft(conn, indexer, project, "links", ["api"], body("Links", tasks=tasks))
        wave = None
        if in_wave:
            wave = await plans.draft_wave(conn, indexer, project, WaveBody(
                title="MVP", goal="g", features=[WaveFeature(plan_id=plan_id)]))
            queued = (await plans.approve_wave(conn, indexer, wave))[plan_id]
        else:
            queued = await plans.approve(conn, indexer, plan_id)
        await conn.commit()
        return plan_id, queued, wave
    return run_db(go)


def finish(run_db, task):
    async def go(conn):
        await rules.transition(conn, task, "running", "runner")
        await rules.transition(conn, task, "done", "runner")
        await conn.commit()
    run_db(go)


def test_abandoning_the_last_open_task_finishes_the_plan_and_its_wave(client, project, run_db, indexer):
    plan_id, (first, second), wave = approved_plan(run_db, indexer, project, in_wave=True)
    finish(run_db, first)
    assert status(run_db, plan_id) == "approved"
    assert abandon(client, project, second).status_code == 200
    assert status(run_db, plan_id) == "done"
    assert status(run_db, wave) == "done"


def test_a_plan_with_every_task_abandoned_is_abandoned_and_closes_its_wave(client, project, run_db, indexer):
    plan_id, (first, second), wave = approved_plan(run_db, indexer, project, in_wave=True)
    assert abandon(client, project, first).status_code == 200
    assert status(run_db, plan_id) == "approved"
    assert abandon(client, project, second).status_code == 200
    assert status(run_db, plan_id) == "abandoned"
    assert status(run_db, wave) == "done"


def test_a_plan_left_complete_before_is_closed_at_the_next_claim(client, project, run_db, indexer):
    plan_id, (first, second), wave = approved_plan(run_db, indexer, project, in_wave=True)

    async def stuck(conn):  # abandoned the way it was before the close ran on abandon
        await rules.transition(conn, first, "running", "runner")
        await rules.transition(conn, first, "done", "runner")
        await rules.transition(conn, second, "abandoned", "human")
        await conn.commit()
    run_db(stuck)
    assert status(run_db, plan_id) == "approved"
    assert client.post(f"/api/projects/{project}/tasks/claim").json()["task"] is None
    assert status(run_db, plan_id) == "done"
    assert status(run_db, wave) == "done"


def test_a_plan_awaiting_its_smoke_test_stays_approved_when_the_last_task_is_abandoned(client, project, run_db, indexer):
    plan_id, (first, second), _ = approved_plan(run_db, indexer, project, smoke_test=True)
    finish(run_db, first)
    assert abandon(client, project, second).status_code == 200
    assert status(run_db, plan_id) == "approved"
    assert client.post(f"/api/projects/{project}/tasks/claim").json()["smoke_test"]["id"] == plan_id
    assert status(run_db, plan_id) == "approved"
