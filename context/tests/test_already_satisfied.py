from concurrent.futures import ThreadPoolExecutor

import pytest

from autonome_context import plans, rules, store
from autonome_context.plans import PlanBody, ProposedGate, TaskSpec

from conftest import add_feature
from test_triage import enable
from test_web import client, status  # noqa: F401  (the client fixture)

SHA = "a3f9c1e0"
JEV = "triage: {model: jev-latest}\n"


def triage(**kw):
    return {"model": "jev-1.14.0", "answers": {"delivered_by_commit": "0.94"}, "verdict": "accept", **kw}


def satisfied(**kw):
    return {"outcome": "already_satisfied", "satisfied_by": SHA, "satisfied_triage": triage(), **kw}


@pytest.fixture
def running(run_db, project, indexer):
    """An approved plan of two tasks, the first running; the second adds a gate with the first."""
    async def go(conn):
        await add_feature(conn, project, "links")
        plan_id = await plans.draft(conn, indexer, project, "links", [], PlanBody(
            title="Links", goal="g", tasks=[TaskSpec(title="First", spec="s")],
            gates=[ProposedGate(name="e2e", run="pnpm test", with_task=1)]))
        [first] = await plans.approve(conn, indexer, plan_id)
        await rules.transition(conn, first, "running", "runner")
        await conn.commit()
        return plan_id, first
    return run_db(go)


def close(client, task, **run):
    return client.post(f"/api/tasks/{task}/transition", json={"to": "done", "run": run})


def test_a_proven_task_is_done_and_the_plan_closes_with_its_last_task(client, project, run_db, indexer, running):
    plan_id, task = running
    enable(run_db, indexer, project, JEV)
    assert close(client, task, **satisfied()).status_code == 200
    assert status(run_db, task) == "done" and status(run_db, plan_id) == "done"
    body = run_db(lambda c: store.get_document(c, task))["body"]
    assert body["runs"][-1]["satisfied_by"] == SHA and "after_merge_review" not in body


@pytest.mark.parametrize("run", [
    {"satisfied_by": None}, {"satisfied_by": "xyz"}, {"satisfied_by": ";rm"}, {"satisfied_by": "a" * 41},
    {"satisfied_triage": None}, {"satisfied_triage": triage(verdict="reject")},
    {"satisfied_triage": triage(verdict="escalate")}, {"satisfied_triage": {"model": "jev-latest", "answers": {}}},
])
def test_without_proof_the_task_stays_running(client, project, run_db, indexer, running, run):
    _, task = running
    enable(run_db, indexer, project, JEV)
    body = {k: v for k, v in {**satisfied(), **run}.items() if v is not None}
    assert close(client, task, **body).status_code == 422
    assert status(run_db, task) == "running"


@pytest.mark.parametrize("yaml", ["gates: []\n", "triage: {model: haiku}\n", "triage: {model: off}\n"])
def test_only_the_jev_triage_accepts_it(client, project, run_db, indexer, running, yaml):
    _, task = running
    enable(run_db, indexer, project, yaml)
    assert close(client, task, **satisfied()).status_code == 403
    assert status(run_db, task) == "running"


def test_a_gate_that_came_with_the_task_joins_the_gate_config(client, project, run_db, indexer, running):
    plan_id, task = running
    enable(run_db, indexer, project, JEV)
    assert close(client, task, **satisfied()).status_code == 200
    latest = run_db(lambda c: store.gate_config_versions(c, project))[0]
    assert "pnpm test" in latest["body"]["yaml"]
    assert latest["body"]["source"] == {"plan_id": plan_id, "task_id": task}


def test_the_task_page_and_the_queue_show_the_sha_escaped(client, project, run_db, indexer, running):
    _, task = running
    enable(run_db, indexer, project, JEV)
    assert close(client, task, **satisfied(satisfied_by="abcdef1")).status_code == 200

    async def mark(conn):  # the validated sha is hex; the template escapes whatever a run holds
        doc = await store.get_document(conn, task)
        runs = [{**doc["body"]["runs"][-1], "satisfied_by": "<b>x</b>"}]
        await store.add_version(conn, None, task, {**doc["body"], "runs": runs})
        await conn.commit()
    run_db(mark)
    for path in (f"/ui/p/{project}/documents/{task}", f"/ui/p/{project}/queue"):
        page = client.get(path).text
        assert "Already satisfied by" in page and "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page, path


def test_insights_list_the_tasks_closed_without_a_merge(client, project, run_db, indexer, running):
    _, task = running
    enable(run_db, indexer, project, JEV)
    close(client, task, **satisfied())
    [item] = client.get(f"/api/projects/{project}/insights").json()["already_satisfied"]
    assert (item["task_id"], item["title"], item["satisfied_by"]) == (task, "First", SHA)


def test_two_equal_transitions_at_once_leave_one_done_version(client, project, run_db, indexer, running):
    _, task = running
    enable(run_db, indexer, project, JEV)
    count = "select count(*) from document_version where document_id = %s"

    async def versions(conn):
        return (await (await conn.execute(count, (task,))).fetchone())[0]
    before = run_db(versions)
    with ThreadPoolExecutor(2) as pool:
        codes = sorted(f.result().status_code for f in [pool.submit(close, client, task, **satisfied()) for _ in range(2)])
    assert codes == [200, 409]
    assert run_db(versions) == before + 1
    assert len(run_db(lambda c: store.get_document(c, task))["body"]["runs"]) == 1
