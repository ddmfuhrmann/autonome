import pytest

from autonome_context import plans, rules, store
from autonome_context.plans import PlanBody, TaskSpec

from conftest import add_feature
from test_web import client, status  # noqa: F401  (the client fixture)


@pytest.fixture
def pending_task(run_db, project, indexer):
    """An approved plan with one pending task, as {project: task id}-making helper."""
    def make(slug):
        async def go(conn):
            await add_feature(conn, slug, "links")
            plan_id = await plans.draft(conn, indexer, slug, "links", [], PlanBody(
                title="Links", goal="g", tasks=[TaskSpec(title="t", spec="s")]))
            [task] = await plans.approve(conn, indexer, plan_id)
            await conn.commit()
            return task
        return run_db(go)

    return make


def pause(client, project, **body):
    return client.post(f"/api/projects/{project}/pause", json={"by": "tester", "reason": "stop", **body})


def test_a_paused_project_hands_out_nothing(client, project, run_db, pending_task):
    task = pending_task(project)
    r = pause(client, project)
    assert r.status_code == 200
    claim = client.post(f"/api/projects/{project}/tasks/claim").json()
    assert claim["task"] is None and claim["paused"]["by"] == "tester" and claim["paused"]["reason"] == "stop"
    assert claim["paused"]["at"]
    assert status(run_db, task) == "pending"


def test_pausing_twice_keeps_the_first_pause(client, project, run_db, pending_task):
    pending_task(project)
    first = pause(client, project, by="first", reason="one").json()["paused"]
    second = pause(client, project, by="second", reason="two").json()["paused"]
    assert second == first
    assert client.get(f"/api/projects/{project}/state").json()["paused"] == first


def test_pausing_an_unknown_project_is_a_404_with_a_message(client):
    r = pause(client, "no-such-project")
    assert r.status_code == 404 and isinstance(r.json()["error"], str)


def test_state_has_paused_null_when_running(client, project, pending_task):
    pending_task(project)
    assert client.get(f"/api/projects/{project}/state").json()["paused"] is None


@pytest.mark.parametrize("reason", ["line\nbreak", "bell\x07", "x" * 501])
def test_a_bad_reason_is_refused(client, project, pending_task, reason):
    pending_task(project)
    assert pause(client, project, reason=reason).status_code == 422
    assert client.get(f"/api/projects/{project}/state").json()["paused"] is None


def test_a_reason_of_500_characters_is_accepted(client, project, pending_task):
    pending_task(project)
    assert pause(client, project, reason="x" * 500).status_code == 200


def test_resume_puts_paused_tasks_back_to_pending_and_clears_the_pause(client, project, run_db, pending_task):
    task = pending_task(project)
    run_db(lambda c: rules.transition(c, task, "running", "runner"))
    assert client.post(f"/api/tasks/{task}/transition", json={"to": "paused"}).status_code == 200
    assert status(run_db, task) == "paused"
    pause(client, project)

    async def resume(conn):
        ids = await store.resume_project(conn, project)
        await conn.commit()
        return ids, await store.project_pause(conn, project)

    ids, pause_after = run_db(resume)
    assert ids == [task] and pause_after is None
    assert status(run_db, task) == "pending"
    assert client.post(f"/api/projects/{project}/tasks/claim").json()["task"]["id"] == task


def test_the_runner_cannot_resume_a_paused_task(client, project, run_db, pending_task):
    task = pending_task(project)
    run_db(lambda c: rules.transition(c, task, "running", "runner"))
    client.post(f"/api/tasks/{task}/transition", json={"to": "paused"})
    r = client.post(f"/api/tasks/{task}/transition", json={"to": "pending"})
    assert r.status_code == 409 and status(run_db, task) == "paused"


def test_a_human_may_abandon_a_paused_task_and_the_runner_may_not(run_db, project, pending_task):
    task = pending_task(project)
    run_db(lambda c: rules.transition(c, task, "running", "runner"))
    run_db(lambda c: rules.transition(c, task, "paused", "runner"))
    with pytest.raises(rules.Forbidden):
        run_db(lambda c: rules.transition(c, task, "abandoned", "runner"))
    run_db(lambda c: rules.transition(c, task, "abandoned", "human"))
    assert status(run_db, task) == "abandoned"


def test_pausing_one_project_does_not_stop_another(client, project, run_db, pending_task):
    other = f"{project}-b"
    pending_task(project)
    other_task = pending_task(other)
    pause(client, project)
    claim = client.post(f"/api/projects/{other}/tasks/claim").json()
    assert claim["task"]["id"] == other_task and "paused" not in claim
