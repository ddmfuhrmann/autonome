import pytest

from autonome_context import plans, rules, server, store, web
from autonome_context.plans import PlanBody, Rehearsal, TaskSpec

from test_triage import enable
from test_waves import play, two_feature_wave
from test_web import FETCH, client, status  # noqa: F401  (the client fixture)

JEV = "triage: {model: jev-latest}\n"


def verdict(v="accept", **kw):
    return {"model": "jev-latest", "state": "ok", "answers": {"drops_acceptance": "0.09", "changes_design": "0.04"},
            "verdict": v, **kw}


def stats_body(tasks: int = 2, **change) -> PlanBody:
    specs = [TaskSpec(title=f"Stats {i}", spec=f"do Stats {i}", acceptance=[f"Stats {i} counts"])
             for i in range(1, tasks + 1)]
    for i, patch in change.items():
        specs[int(i[1:]) - 1] = specs[int(i[1:]) - 1].model_copy(update=patch)
    return PlanBody(title="Stats", goal="Stats goal", out_of_scope=["rate limit"], tasks=specs)


@pytest.fixture
def held(run_db, project, indexer):
    """The stats plan held by the rehearsal with a revision. Returns (plan id, old task ids)."""
    async def go(conn, tasks=2, notes="the catch-all route breaks the test", **change):
        links, stats, wave = await two_feature_wave(conn, indexer, project)
        queued = await plans.approve_wave(conn, indexer, wave)
        await play(conn, queued[links][0])
        await store.finish_plan_if_complete(conn, links)
        plan = await store.get_document(conn, stats)
        original = stats_body()
        await store.add_version(conn, indexer, stats, {**plan["body"], **original.model_dump(mode="json")})
        await plans.rehearse(conn, indexer, stats, Rehearsal(
            verdict="revise", notes=notes, plan=stats_body(tasks, **change)))
        await conn.commit()
        return stats, queued[stats]

    return lambda **kw: run_db(lambda conn: go(conn, **kw))


def revisions(client, project):
    return client.get(f"/api/projects/{project}/triage").json()["revisions"]


def url(plan_id):
    return f"/api/plans/{plan_id}/rehearsal-triage"


def test_revisions_carry_only_the_changed_tasks_with_their_position(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, JEV)
    plan_id, _ = held(t2={"spec": "do Stats 2 with the new route", "acceptance": ["Stats 2 counts", "route order"]},
                      notes="n" * 4000)
    [item] = revisions(client, project)
    assert item["plan_id"] == plan_id and item["shape_changed"] is False
    s = item["state"]
    assert s["plan"]["goal"] == "Stats goal" and s["plan"]["out_of_scope"] == ["rate limit"]
    assert [t["position"] for t in s["original"]] == [2] == [t["position"] for t in s["revised"]]
    assert s["original"][0]["spec"] == "do Stats 2" and s["revised"][0]["spec"] == "do Stats 2 with the new route"
    assert len(s["notes"]) == 3000


def test_a_task_added_changes_the_shape(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, JEV)
    held(tasks=3)
    [item] = revisions(client, project)
    assert item["shape_changed"] is True
    assert [t["position"] for t in item["state"]["revised"]] == [3] and item["state"]["original"] == []


def test_accept_approves_as_the_runner_and_queues_the_revised_tasks(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, JEV)
    plan_id, old = held(t2={"spec": "do Stats 2 better"})
    r = client.post(url(plan_id), json=verdict("accept"))
    assert r.status_code == 200 and r.json() == {"status": "approved"}
    plan = run_db(lambda c: store.get_document(c, plan_id))
    assert plan["status"] == "approved" and plan["body"]["preflight"]["verdict"] == "revised"
    t = plan["body"]["preflight"]["triage"]
    assert t["verdict"] == "accept" and t["model"] == "jev-latest" and t["at"] and t["answers"]["drops_acceptance"] == "0.09"
    tasks = run_db(lambda c: store.list_documents(c, project, type="task", status="pending", limit=None))
    assert sorted(x["body"]["spec"] for x in tasks if x["body"]["plan_id"] == plan_id) == ["do Stats 1", "do Stats 2 better"]
    assert all(status(run_db, i) == "abandoned" for i in old)
    assert revisions(client, project) == []


def test_hold_keeps_the_plan_held_and_leaves_the_pending_list(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, JEV)
    plan_id, old = held(t2={"spec": "do Stats 2 better"})
    r = client.post(url(plan_id), json=verdict("hold", answers={"changes_design": "0.94"}))
    assert r.status_code == 200 and r.json() == {"status": "held"}
    plan = run_db(lambda c: store.get_document(c, plan_id))
    assert plan["status"] == "held" and plan["body"]["preflight"]["triage"]["verdict"] == "hold"
    assert revisions(client, project) == []
    assert all(status(run_db, i) == "pending" for i in old)
    assert client.post(url(plan_id), json=verdict("accept")).status_code == 403  # judged once


def test_a_held_verdict_shows_in_the_queue_detail(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, JEV)
    plan_id, _ = held(t2={"spec": "do Stats 2 better"})
    client.post(url(plan_id), json=verdict("hold", answers={"adds_out_of_scope": "0.88", "changes_design": "0.10"}))
    [item] = [i for i in run_db(lambda c: web._queue(c, project)) if i["kind"] == "held"]
    assert "triage: hold (adds_out_of_scope 0.88)" in run_db(lambda c: server._detail(c, "held", item["doc"]))
    assert "triage: hold (adds_out_of_scope 0.88)" in client.get(f"/ui/p/{project}/approvals").text


def test_the_state_snapshot_names_the_flag_that_held_a_revision(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, JEV)
    plan_id, _ = held(t2={"spec": "do Stats 2 better"})

    def held_items():
        return [i for i in client.get(f"/api/projects/{project}/state").json()["waiting"] if i["kind"] == "held"]

    [item] = held_items()  # no verdict yet
    assert item["id"] == plan_id and item["triage_hold"] is None
    client.post(url(plan_id), json=verdict("hold", answers={"adds_out_of_scope": "0.88", "changes_design": "0.10"}))
    [item] = held_items()
    assert item["id"] == plan_id and item["blocks"] is True and item["triage_hold"] == "adds_out_of_scope"


@pytest.mark.parametrize("yaml", [None, "triage: {model: off}\n", "triage: {model: haiku}\n"])
def test_without_jev_the_endpoint_refuses_and_changes_nothing(client, project, run_db, indexer, held, yaml):
    if yaml:
        enable(run_db, indexer, project, yaml)
    plan_id, _ = held(t2={"spec": "do Stats 2 better"})
    before = run_db(lambda c: store.get_document(c, plan_id))
    assert client.post(url(plan_id), json=verdict("accept")).status_code == 403
    assert client.post(url(plan_id), json=verdict("accept", model="haiku")).status_code == 403
    after = run_db(lambda c: store.get_document(c, plan_id))
    assert (after["status"], after["version"]) == ("held", before["version"])


def test_a_verdict_from_another_model_is_refused_with_jev_on(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, JEV)
    plan_id, _ = held(t2={"spec": "do Stats 2 better"})
    before = run_db(lambda c: store.get_document(c, plan_id))
    assert client.post(url(plan_id), json=verdict("accept", model="haiku")).status_code == 403
    after = run_db(lambda c: store.get_document(c, plan_id))
    assert (after["status"], after["version"]) == ("held", before["version"])


def test_a_verdict_names_the_jev_that_answered_and_is_kept_as_such(client, project, run_db, indexer, held):
    # The runner sends the model Jev's API answered with (shortlink-v7's P771 got a 403 for `jev-1.14.0`).
    enable(run_db, indexer, project, JEV)
    plan_id, _ = held(t2={"spec": "do Stats 2 better"})
    assert client.post(url(plan_id), json=verdict("accept", model="jev-1.14.0")).status_code == 200
    plan = run_db(lambda c: store.get_document(c, plan_id))
    assert plan["status"] == "approved" and plan["body"]["preflight"]["triage"]["model"] == "jev-1.14.0"


def test_two_verdicts_at_once_let_only_one_through(client, project, run_db, indexer, held):
    from concurrent.futures import ThreadPoolExecutor

    enable(run_db, indexer, project, JEV)
    plan_id, _ = held(t2={"spec": "do Stats 2 better"})
    with ThreadPoolExecutor(2) as pool:
        codes = sorted(f.result().status_code for f in [
            pool.submit(client.post, url(plan_id), json=verdict("accept")) for _ in range(2)])
    assert codes == [200, 403]
    plan = run_db(lambda c: store.get_document(c, plan_id))
    assert plan["status"] == "approved" and plan["body"]["preflight"]["triage"]["verdict"] == "accept"
    assert plan["version"] == run_db(lambda c: store.get_document(c, plan_id))["version"]


def test_a_model_other_than_jev_in_the_config_refuses_even_for_a_jev_verdict(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, "triage: {model: haiku}\n")
    plan_id, _ = held(t2={"spec": "x"})
    assert client.post(url(plan_id), json=verdict("accept")).status_code == 403
    assert status(run_db, plan_id) == "held"


def test_a_plan_the_human_already_resolved_is_refused(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, JEV)
    plan_id, _ = held(t2={"spec": "do Stats 2 better"})
    r = client.post(f"/ui/p/{project}/documents/{plan_id}/transition", data={"to": "approved"}, headers=FETCH)
    assert r.status_code == 200
    version = run_db(lambda c: store.get_document(c, plan_id))["version"]
    assert client.post(url(plan_id), json=verdict("accept")).status_code == 403
    after = run_db(lambda c: store.get_document(c, plan_id))
    assert after["status"] == "approved" and after["version"] == version


def test_the_human_still_resolves_a_plan_judged_hold(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, JEV)
    plan_id, _ = held(t2={"spec": "do Stats 2 better"})
    client.post(url(plan_id), json=verdict("hold"))
    r = client.post(f"/ui/p/{project}/documents/{plan_id}/transition", data={"to": "revised"}, headers=FETCH)
    assert r.status_code == 200
    plan = run_db(lambda c: store.get_document(c, plan_id))
    assert plan["status"] == "approved" and plan["body"]["preflight"]["verdict"] == "revised"


def test_the_human_keeps_the_original_of_a_plan_judged_hold(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, JEV)
    plan_id, old = held(t2={"spec": "do Stats 2 better"})
    client.post(url(plan_id), json=verdict("hold"))
    r = client.post(f"/ui/p/{project}/documents/{plan_id}/transition", data={"to": "approved"}, headers=FETCH)
    assert r.status_code == 200
    plan = run_db(lambda c: store.get_document(c, plan_id))
    assert plan["status"] == "approved" and plan["body"]["preflight"]["verdict"] == "kept"
    assert all(status(run_db, i) == "pending" for i in old)


def test_only_the_human_or_runner_move_a_held_plan(run_db, project, held):
    plan_id, _ = held(t2={"spec": "x"})

    async def go(conn):
        with pytest.raises(rules.Forbidden):
            await rules.transition(conn, plan_id, "approved", "agent")
    run_db(go)
