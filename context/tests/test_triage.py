import pytest
from pydantic import ValidationError

from autonome_context import store
from autonome_context.gates import GateConfig, Triage

from conftest import make_handoff
from test_web import FETCH, client, status  # noqa: F401  (the client fixture)

ON = "triage: {model: haiku, max_rounds: 2}\n"


def verdict(v, round=1):
    return {"backend": "haiku", "model": "claude-haiku-5-5", "state": "ok",
            "answers": {"changes_behaviour": "no"}, "verdict": v, "round": round}


@pytest.fixture
def deviation(run_db, project, indexer):
    from autonome_context.handoff import Decision

    async def go(conn, deviation=True):
        await store.put_handoff(conn, indexer, make_handoff(project, feature="links", decisions=[
            Decision(title="Dict instead of a response model", rationale="faster", deviation=deviation)]), None)
        await conn.commit()
        [d] = await store.list_documents(conn, project, type="decision")
        return d["id"]

    return lambda **kw: run_db(lambda conn: go(conn, **kw))


def enable(run_db, indexer, project, yaml=ON):
    async def go(conn):
        async with conn.transaction():
            await store.save_singleton(conn, indexer, project, "gate_config", {"yaml": yaml})
    run_db(go)


def test_triage_config_validates_and_defaults_to_none():
    assert GateConfig().triage is None
    t = GateConfig.from_yaml("triage: {}\n").triage
    assert (t.model, t.fallback, t.reject_at, t.max_rounds) == ("jev-latest", "haiku", 0.7, 2)
    for bad in ({"reject_at": 0}, {"reject_at": 1.1}, {"max_rounds": 0}, {"model": "gpt"}):
        with pytest.raises(ValidationError):
            Triage(**bad)


def test_without_triage_config_the_endpoint_refuses_and_changes_nothing(client, project, run_db, deviation):
    doc_id = deviation()
    r = client.post(f"/api/decisions/{doc_id}/triage", json=verdict("accept"))
    assert r.status_code == 403
    assert status(run_db, doc_id) == "proposed"
    assert run_db(lambda c: store.get_document(c, doc_id))["version"] == 1


def test_triage_off_refuses(client, project, run_db, indexer, deviation):
    enable(run_db, indexer, project, "triage: {model: off}\n")
    assert client.post(f"/api/decisions/{deviation()}/triage", json=verdict("accept")).status_code == 403


def test_accept_moves_proposed_to_accepted_and_keeps_the_verdict(client, project, run_db, indexer, deviation):
    enable(run_db, indexer, project)
    doc_id = deviation()
    r = client.post(f"/api/decisions/{doc_id}/triage", json=verdict("accept"))
    assert r.status_code == 200 and r.json() == {"status": "accepted"}
    d = run_db(lambda c: store.get_document(c, doc_id))
    assert d["status"] == "accepted"
    [entry] = d["body"]["triage_history"]
    assert entry["verdict"] == "accept" and entry["round"] == 1 and entry["backend"] == "haiku" and entry["at"]
    assert d["body"]["triage"] == entry


def test_reject_queues_an_undo_task_like_the_human_path(client, project, run_db, indexer, deviation):
    enable(run_db, indexer, project)
    doc_id = deviation()
    r = client.post(f"/api/decisions/{doc_id}/triage", json=verdict("reject"))
    assert r.status_code == 200 and r.json() == {"status": "rejected"}
    assert status(run_db, doc_id) == "rejected"
    [undo] = run_db(lambda c: store.list_documents(c, project, type="task"))
    assert undo["body"]["title"] == "Undo: Dict instead of a response model"
    assert undo["body"]["source"] == f"decision:{doc_id}" and undo["feature"] == "links"


def test_a_decision_that_is_not_a_deviation_or_not_proposed_is_refused(client, project, run_db, indexer, deviation):
    enable(run_db, indexer, project)
    assert client.post(f"/api/decisions/{deviation(deviation=False)}/triage", json=verdict("reject")).status_code == 403
    assert run_db(lambda c: store.list_documents(c, project, type="task")) == []

    async def other(conn):
        from autonome_context.handoff import Decision
        await store.put_handoff(conn, indexer, make_handoff(project, feature="links", session_id="s2", decisions=[
            Decision(title="Another one", rationale="r", deviation=True)]), None)
        await conn.commit()
        return next(d["id"] for d in await store.list_documents(conn, project, type="decision")
                    if d["body"]["title"] == "Another one")
    other_id = run_db(other)
    assert client.post(f"/api/decisions/{other_id}/triage", json=verdict("accept")).status_code == 200
    assert client.post(f"/api/decisions/{other_id}/triage", json=verdict("reject")).status_code == 403
    assert status(run_db, other_id) == "accepted"


def test_reject_at_the_last_round_keeps_it_proposed_and_marks_it_exhausted(client, project, run_db, indexer, deviation):
    enable(run_db, indexer, project)
    doc_id = deviation()
    r = client.post(f"/api/decisions/{doc_id}/triage", json=verdict("reject", round=2))
    assert r.status_code == 200 and r.json()["status"] == "proposed"
    d = run_db(lambda c: store.get_document(c, doc_id))
    assert d["status"] == "proposed" and d["body"]["triage_exhausted"] is True
    assert d["body"]["triage_history"][0]["verdict"] == "reject"
    assert run_db(lambda c: store.list_documents(c, project, type="task")) == []


def test_malformed_input_is_a_422_and_changes_nothing(client, project, run_db, indexer, deviation):
    enable(run_db, indexer, project)
    doc_id = deviation()
    url = f"/api/decisions/{doc_id}/triage"
    assert client.post(url, content=b"{not json", headers={"content-type": "application/json"}).status_code == 422
    assert client.post(url, json={**verdict("accept"), "answers": {"q": "a long free text answer"}}).status_code == 422
    assert client.post(url, json={**verdict("accept"), "answers": {"q": ""}}).status_code == 422
    assert client.post(url, json={**verdict("accept"), "verdict": "maybe"}).status_code == 422
    assert status(run_db, doc_id) == "proposed"


def test_a_second_verdict_on_a_decided_deviation_is_refused_not_a_500(client, project, run_db, indexer, deviation):
    enable(run_db, indexer, project)
    doc_id = deviation()
    assert client.post(f"/api/decisions/{doc_id}/triage", json=verdict("reject")).status_code == 200
    assert client.post(f"/api/decisions/{doc_id}/triage", json=verdict("accept")).status_code == 403


def seed(run_db, indexer, project, *, session="s-1", title="Dict", source=None, with_task=True):
    """A handoff with one deviation, reporting a task of a plan. Returns (decision id, task id)."""
    from autonome_context.handoff import Decision

    async def go(conn):
        task_id = None
        if with_task:
            plan_id = await store.create_document(conn, indexer, project=project, type="plan", status="approved",
                feature="links", body={"title": "P", "goal": "Short links", "out_of_scope": ["analytics"], "tasks": []})
            body = {"title": "Task " + title, "spec": "do it", "acceptance": ["works"], "plan_id": plan_id,
                    "runs": [{"review": {"findings": [{"severity": "medium", "kind": "style", "issue": "naming"}]}}]}
            if source:
                body["source"] = source
            task_id = await store.create_document(conn, indexer, project=project, type="task", status="playing",
                                                  feature="links", layers=["api"], body=body)
        h = await store.put_handoff(conn, indexer, make_handoff(project, feature="links", layers=["api"],
            session_id=session, decisions=[Decision(title=title, rationale="faster", deviation=True)]), None)
        if task_id:
            await store.link(conn, h, task_id, "reports")
            # An earlier round's failure of a gate the last run did not repeat must not show up.
            await store.record_gate_runs(conn, project, "old-" + session, [
                {"gate": "lint", "passed": False, "duration_ms": 1}], task_id=task_id)
            await store.record_gate_runs(conn, project, session, [
                {"gate": "pytest", "passed": False, "duration_ms": 1}, {"gate": "pytest", "passed": True, "duration_ms": 1},
                {"gate": "ruff", "passed": True, "duration_ms": 1}], task_id=task_id)
        await store.create_document(conn, indexer, project=project, type="convention", status="active",
                                    layers=["api"], body={"title": "Models", "rule": "use response models"})
        await conn.commit()
        d = next(d for d in await store.list_documents(conn, project, type="decision") if d["body"]["title"] == title)
        return d["id"], task_id
    return run_db(go)


def pending(client, project):
    return client.get(f"/api/projects/{project}/triage").json()


def test_state_of_a_deviation_with_a_plan_has_every_field(client, project, run_db, indexer):
    decision, _ = seed(run_db, indexer, project)
    [item] = pending(client, project)
    assert (item["decision_id"], item["round"]) == (decision, 0)
    s = item["state"]
    assert s["deviation"] == {"title": "Dict", "decision": "faster"}
    assert s["task"] == {"title": "Task Dict", "spec": "do it", "acceptance": ["works"]}
    assert s["plan"] == {"goal": "Short links", "out_of_scope": ["analytics"]}
    assert s["conventions"] == [{"title": "Models", "rule": "use response models"}]
    assert sorted((g["name"], g["passed"]) for g in s["gates"]) == [("pytest", True), ("ruff", True)]
    assert s["review"] == [{"severity": "medium", "kind": "style", "issue": "naming"}]


def test_a_decision_with_no_task_returns_only_deviation_and_conventions(client, project, run_db, indexer):
    seed(run_db, indexer, project, with_task=False)
    [item] = pending(client, project)
    assert sorted(item["state"]) == ["conventions", "deviation"]


def test_empty_parts_are_left_out_not_sent_empty(client, project, run_db, indexer):
    _, task_id = seed(run_db, indexer, project)

    async def blank(conn):
        t = await store.get_document(conn, task_id)
        await store.add_version(conn, None, task_id, {"plan_id": t["body"]["plan_id"]})
        await conn.commit()
    run_db(blank)
    [item] = pending(client, project)
    assert "task" not in item["state"] and "deviation" in item["state"]


def test_an_undo_tasks_deviation_is_one_round_later(client, project, run_db, indexer):
    first, _ = seed(run_db, indexer, project, title="First")
    second, _ = seed(run_db, indexer, project, session="s-2", title="Second", source=f"decision:{first}")
    third, _ = seed(run_db, indexer, project, session="s-3", title="Third", source=f"decision:{second}")
    assert {i["decision_id"]: i["round"] for i in pending(client, project)} == {first: 0, second: 1, third: 2}


def test_decided_exhausted_and_already_judged_decisions_are_not_returned(client, project, run_db, indexer):
    enable(run_db, indexer, project)
    accepted, _ = seed(run_db, indexer, project, session="a", title="A")
    rejected, _ = seed(run_db, indexer, project, session="b", title="B")
    exhausted, _ = seed(run_db, indexer, project, session="c", title="C")
    judged, _ = seed(run_db, indexer, project, session="d", title="D")
    assert client.post(f"/api/decisions/{accepted}/triage", json=verdict("accept")).status_code == 200
    assert client.post(f"/api/decisions/{rejected}/triage", json=verdict("reject")).status_code == 200
    assert client.post(f"/api/decisions/{exhausted}/triage", json=verdict("reject", round=2)).status_code == 200
    assert [i["decision_id"] for i in pending(client, project)] == [judged]


def test_history_shows_who_judged_a_deviation_and_the_verdict(client, project, run_db, indexer):
    from autonome_context import history
    enable(run_db, indexer, project)
    by_runner, _ = seed(run_db, indexer, project, session="a", title="A")
    waiting, _ = seed(run_db, indexer, project, session="b", title="B")
    by_human, _ = seed(run_db, indexer, project, session="c", title="C")
    assert client.post(f"/api/decisions/{by_runner}/triage", json=verdict("accept")).status_code == 200
    client.post(f"/ui/p/{project}/documents/{by_human}/transition", data={"to": "accepted"}, headers=FETCH)

    h = run_db(lambda c: history.build(c, project, "links", indexer.embedder, feature="links"))
    items = {i["decision"]: i for i in h["deviation_decisions"]}
    assert (items[by_runner]["judged_by"], items[by_runner]["verdict"]) == ("haiku", "accept")
    assert items[by_human]["judged_by"] == "human" and "verdict" not in items[by_human]
    assert "judged_by" not in items[waiting]


def test_insights_list_exhausted_and_undone_deviations_with_the_undo_gate_result(client, project, run_db, indexer):
    from autonome_context import insights, rules
    enable(run_db, indexer, project)
    undone, _ = seed(run_db, indexer, project, session="a", title="Undone")
    exhausted, _ = seed(run_db, indexer, project, session="b", title="Exhausted")
    failed, _ = seed(run_db, indexer, project, session="c", title="Failed")
    norun, _ = seed(run_db, indexer, project, session="d", title="NoRun")
    for d in (undone, failed, norun):
        assert client.post(f"/api/decisions/{d}/triage", json=verdict("reject")).status_code == 200
    assert client.post(f"/api/decisions/{exhausted}/triage", json=verdict("reject", round=2)).status_code == 200

    async def finish(conn):
        undo_of = {}
        for t in await store.list_documents(conn, project, type="task"):
            source = str(t["body"].get("source", ""))
            if source.startswith("decision:"):
                undo_of[int(source[9:])] = t["id"]
                await rules.transition(conn, t["id"], "playing", "runner")
                await rules.transition(conn, t["id"], "done", "runner")
        # An older session failed; only the newest session's latest result counts.
        await store.record_gate_runs(conn, project, "undo-old", [
            {"gate": "pytest", "passed": False, "duration_ms": 1}], task_id=undo_of[undone])
        await store.record_gate_runs(conn, project, "undo-a", [
            {"gate": "pytest", "passed": True, "duration_ms": 1}], task_id=undo_of[undone])
        await store.record_gate_runs(conn, project, "undo-b", [
            {"gate": "ruff", "passed": True, "duration_ms": 1},
            {"gate": "pytest", "passed": False, "duration_ms": 1}], task_id=undo_of[failed])
        await conn.commit()
        return undo_of, await insights.build(conn, project, indexer.embedder)
    undo_of, data = run_db(finish)

    t = data["triage"]
    assert t["by_verdict"] == {"reject": 4} and t["by_backend"] == {"haiku": 4}
    assert t["exhausted"] == [{"decision_id": exhausted, "title": "Exhausted", "feature": "links"}]
    assert {u["decision_id"]: (u["task_id"], u["gates_passed"], u["judged_by"]) for u in t["undone"]} == {
        undone: (undo_of[undone], True, "haiku"), failed: (undo_of[failed], False, "haiku"),
        norun: (undo_of[norun], None, "haiku")}
    assert t["recurring"] == [{"feature": "links", "rejected": 3}]


def test_a_human_settling_an_exhausted_deviation_is_the_judge(client, project, run_db, indexer):
    from autonome_context import history, insights
    enable(run_db, indexer, project)
    exhausted, _ = seed(run_db, indexer, project, title="Exhausted")
    assert client.post(f"/api/decisions/{exhausted}/triage", json=verdict("reject", round=2)).status_code == 200
    client.post(f"/ui/p/{project}/documents/{exhausted}/transition", data={"to": "accepted"}, headers=FETCH)
    assert status(run_db, exhausted) == "accepted"

    async def read(conn):
        return (await history.build(conn, project, "links", indexer.embedder, feature="links"),
                await insights.build(conn, project, indexer.embedder))
    h, data = run_db(read)
    [item] = h["deviation_decisions"]
    assert item["judged_by"] == "human" and "verdict" not in item
    assert data["triage"]["by_verdict"] == {} and data["triage"]["by_backend"] == {}


def test_the_human_path_is_unchanged(client, project, run_db, indexer, deviation):
    enable(run_db, indexer, project)
    doc_id = deviation()
    r = client.post(f"/ui/p/{project}/documents/{doc_id}/transition", data={"to": "accepted"}, headers=FETCH)
    assert r.status_code == 200 and r.json()["status"] == "accepted"
