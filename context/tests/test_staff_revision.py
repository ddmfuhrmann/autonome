import pytest

from autonome_context import plans, rules, store
from autonome_context.plans import PlanBody, TaskSpec

from conftest import add_feature
from test_triage import enable
from test_web import client, status  # noqa: F401  (the client fixture)

JEV = "triage: {model: jev-latest}\n"


def revision(**kw):
    revised = {"title": "First v2", "spec": "new spec\n\n**Where**\n- a.py", "acceptance": ["it works"]}
    return {"revised": {**revised, **kw.pop("revised", {})}, "diagnosis": "the spec was wrong", "model": "jev-1.14.0",
            "answers": {"changes_design": "0.1"}, "verdict": "accept", **kw}


@pytest.fixture
def running(run_db, project, indexer):
    async def go(conn):
        await add_feature(conn, project, "links")
        plan_id = await plans.draft(conn, indexer, project, "links", [], PlanBody(
            title="Links", goal="g", tasks=[TaskSpec(title="First", spec="old spec", acceptance=["old"])]))
        [first] = await plans.approve(conn, indexer, plan_id)
        await rules.transition(conn, first, "running", "runner")
        await conn.commit()
        return first
    return run_db(go)


def post(client, task, body):
    return client.post(f"/api/tasks/{task}/staff-revision", json=body)


def body_of(run_db, task):
    return run_db(lambda c: store.get_document(c, task))["body"]


ORIGINAL = {"title": "First", "spec": "old spec", "acceptance": ["old"]}


def test_accept_swaps_the_spec_and_keeps_the_original(client, project, run_db, indexer, running):
    enable(run_db, indexer, project, JEV)
    assert post(client, running, revision()).status_code == 200
    b = body_of(run_db, running)
    assert (b["title"], b["spec"], b["acceptance"]) == ("First v2", "new spec\n\n**Where**\n- a.py", ["it works"])
    sr = b["staff_revision"]
    assert sr["original"] == ORIGINAL and sr["verdict"] == "accept" and sr["diagnosis"] == "the spec was wrong"
    assert sr["model"] == "jev-1.14.0" and sr["answers"] == {"changes_design": "0.1"} and sr["at"]
    assert status(run_db, running) == "running"


def test_hold_keeps_the_proposal_and_the_spec(client, project, run_db, indexer, running):
    enable(run_db, indexer, project, JEV)
    assert post(client, running, revision(verdict="hold")).status_code == 200
    b = body_of(run_db, running)
    assert (b["title"], b["spec"], b["acceptance"]) == ("First", "old spec", ["old"])
    assert b["staff_revision"]["verdict"] == "hold" and b["staff_revision"]["revised"]["title"] == "First v2"


@pytest.mark.parametrize("yaml", ["gates: []\n", "triage: {model: haiku}\n", "triage: {model: off}\n"])
def test_without_the_jev_triage_nothing_changes(client, project, run_db, indexer, running, yaml):
    enable(run_db, indexer, project, yaml)
    assert post(client, running, revision()).status_code == 403
    assert "staff_revision" not in body_of(run_db, running)


def test_a_task_that_is_not_running_is_refused(client, project, run_db, indexer, running):
    enable(run_db, indexer, project, JEV)
    run_db(lambda c: rules.transition(c, running, "blocked", "runner"))
    assert post(client, running, revision()).status_code == 403
    assert "staff_revision" not in body_of(run_db, running)


@pytest.mark.parametrize("body", [
    revision(revised={"spec": "a\x00b"}), revision(revised={"spec": ""}), revision(revised={"spec": "x" * 20001}),
    revision(revised={"title": "a\nb"}), revision(revised={"acceptance": [""]}), revision(diagnosis=""),
    revision(model="haiku"), revision(verdict="reject"),
])
def test_bad_fields_are_422_and_nothing_changes(client, project, run_db, indexer, running, body):
    enable(run_db, indexer, project, JEV)
    assert post(client, running, body).status_code == 422
    assert "staff_revision" not in body_of(run_db, running)


def test_the_task_page_shows_the_revision_escaped(client, project, run_db, indexer, running):
    enable(run_db, indexer, project, JEV)
    post(client, running, revision(diagnosis="<b>bad</b>", revised={"spec": "<script>x</script>"}))
    page = client.get(f"/ui/p/{project}/documents/{running}").text
    assert "Staff revision" in page and "&lt;b&gt;bad&lt;/b&gt;" in page and "&lt;script&gt;x&lt;/script&gt;" in page
    assert "<b>bad</b>" not in page and "<script>x</script>" not in page
