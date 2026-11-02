import pytest

from autonome_context import plans, rules, store
from autonome_context.plans import PlanBody, TaskSpec

from conftest import add_feature
from test_web import FETCH, client, status  # noqa: F401  (the client fixture)

PR = "https://github.com/acme/app/pull/42"


@pytest.fixture
def two_tasks(run_db, project, indexer):
    """An approved plan: task 2 depends on task 1, which is running."""
    async def go(conn):
        await add_feature(conn, project, "links")
        plan_id = await plans.draft(conn, indexer, project, "links", [], PlanBody(
            title="Links", goal="g", tasks=[TaskSpec(title="First", spec="s"), TaskSpec(title="Second", spec="s", depends_on=[1])]))
        first, second = await plans.approve(conn, indexer, plan_id)
        await rules.transition(conn, first, "running", "runner")
        await conn.commit()
        return first, second
    return run_db(go)


def move(client, task, to, **run):
    return client.post(f"/api/tasks/{task}/transition", json={"to": to, "run": run} if run else {"to": to})


def open_pr(client, task, mode="manual", url=PR):
    return move(client, task, "in_pr", outcome="pr_opened", pr_url=url, pr_mode=mode)


@pytest.mark.parametrize("run", [
    {}, {"pr_mode": "manual"}, {"pr_url": PR}, {"pr_url": PR, "pr_mode": "sometimes"},
    {"pr_url": "https://gitlab.com/acme/app/-/merge_requests/1", "pr_mode": "manual"},
    {"pr_url": "http://github.com/acme/app/pull/1", "pr_mode": "manual"},
    {"pr_url": "javascript:alert(1)", "pr_mode": "manual"},
])
def test_in_pr_needs_a_github_pr_url_and_a_mode(client, run_db, two_tasks, run):
    first, _ = two_tasks
    r = client.post(f"/api/tasks/{first}/transition", json={"to": "in_pr", "run": run} if run else {"to": "in_pr"})
    assert r.status_code == 422
    assert status(run_db, first) == "running"


def test_a_task_that_depends_on_an_in_pr_one_is_not_claimed_until_it_is_done(client, project, run_db, two_tasks):
    first, second = two_tasks
    assert open_pr(client, first).status_code == 200
    assert status(run_db, first) == "in_pr"
    assert client.post(f"/api/projects/{project}/tasks/claim").json()["task"] is None

    assert move(client, first, "done").status_code == 422  # a merge needs its sha
    assert status(run_db, first) == "in_pr"
    assert move(client, first, "done", outcome="merged", merged_sha="a3f9c1e0").status_code == 200
    assert client.post(f"/api/projects/{project}/tasks/claim").json()["task"]["id"] == second


def test_a_closed_pr_blocks_the_task_and_only_the_human_plays_it_again(client, project, run_db, two_tasks):
    first, _ = two_tasks
    open_pr(client, first)
    assert client.post(f"/ui/p/{project}/documents/{first}/transition", data={"to": "done"}, headers=FETCH).status_code == 409
    assert move(client, first, "blocked", outcome="blocked", reason="the pull request was closed").status_code == 200
    assert status(run_db, first) == "blocked"
    assert move(client, first, "pending").status_code == 409  # the runner may not

    r =client.post(f"/ui/p/{project}/documents/{first}/transition", data={"to": "pending"}, headers=FETCH)
    assert r.status_code == 200 and status(run_db, first) == "pending"


def test_only_the_human_moves_an_in_pr_task_back_to_pending(client, project, run_db, two_tasks):
    first, _ = two_tasks
    open_pr(client, first)
    assert move(client, first, "pending").status_code == 409
    assert status(run_db, first) == "in_pr"
    r = client.post(f"/ui/p/{project}/documents/{first}/transition", data={"to": "pending"}, headers=FETCH)
    assert r.status_code == 200 and status(run_db, first) == "pending"


def test_state_lists_a_manual_pr_as_waiting_with_its_link_and_an_auto_one_as_in_progress(client, project, run_db, two_tasks):
    first, _ = two_tasks
    open_pr(client, first, mode="manual")
    s = client.get(f"/api/projects/{project}/state").json()
    [item] = [w for w in s["waiting"] if w["kind"] == "in_pr"]
    assert (item["id"], item["pr_url"], item["blocks"]) == (first, PR, True)
    assert [t for t in s["tasks"] if t["id"] == first][0]["pr_url"] == PR

    async def back(conn):
        await store.set_status(conn, first, "running")
        await conn.commit()
    run_db(back)
    open_pr(client, first, mode="auto", url="https://github.com/acme/app/pull/43")
    s = client.get(f"/api/projects/{project}/state").json()
    assert not [w for w in s["waiting"] if w["kind"] == "in_pr"]
    task = [t for t in s["tasks"] if t["id"] == first][0]
    assert (task["status"], task["pr_mode"]) == ("in_pr", "auto")


def test_the_queue_and_the_task_page_link_the_pr_escaped(client, project, run_db, two_tasks):
    first, _ = two_tasks
    open_pr(client, first, url="https://github.com/acme/app/pull/42?a=1&b=2")
    for path in (f"/ui/p/{project}/queue", f"/ui/p/{project}/documents/{first}"):
        page = client.get(path).text
        assert 'href="https://github.com/acme/app/pull/42?a=1&amp;b=2"' in page, path
        assert 'rel="noopener noreferrer"' in page
    assert "waiting on you to merge" in client.get(f"/ui/p/{project}/queue").text
