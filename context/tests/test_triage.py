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


def test_the_human_path_is_unchanged(client, project, run_db, indexer, deviation):
    enable(run_db, indexer, project)
    doc_id = deviation()
    r = client.post(f"/ui/p/{project}/documents/{doc_id}/transition", data={"to": "accepted"}, headers=FETCH)
    assert r.status_code == 200 and r.json()["status"] == "accepted"
