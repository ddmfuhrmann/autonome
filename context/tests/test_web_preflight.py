import pytest

from autonome_context import store
from autonome_context.web import preflight_outcome

from test_revision_triage import JEV, held, url, verdict  # noqa: F401  (the held fixture)
from test_triage import enable
from test_web import client  # noqa: F401


@pytest.mark.parametrize("preflight, expected", [
    ({"verdict": "revised", "triage": {"verdict": "accept"}}, "Accepted by the triage"),
    ({"verdict": "revised"}, "Decided by you: revision accepted"),
    ({"verdict": "revised", "triage": {"verdict": "hold"}}, "Decided by you: revision accepted"),
    ({"verdict": "kept", "triage": {"verdict": "hold"}}, "Decided by you: original kept"),
    ({"verdict": "held"}, "Waiting on you"),
    ({"verdict": "ready"}, None),
    ({}, None),
    (None, None),
])
def test_preflight_outcome(preflight, expected):
    assert preflight_outcome(preflight) == expected


def page(client, project, plan_id):
    r = client.get(f"/ui/p/{project}/documents/{plan_id}")
    assert r.status_code == 200
    return r.text


def test_the_page_of_a_plan_the_triage_approved_says_so(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, JEV)
    plan_id, _ = held(t2={"spec": "do Stats 2 better"})
    assert client.post(url(plan_id), json=verdict("accept")).status_code == 200
    html = page(client, project, plan_id)
    assert "Accepted by the triage" in html and "jev-latest" in html
    assert "drops_acceptance: 0.09" in html and "changes_design: 0.04" in html
    assert '<time datetime="' in html.split('id="preflight"')[1]


def test_a_non_numeric_answer_does_not_break_the_page(client, project, run_db, indexer, held):
    enable(run_db, indexer, project, JEV)
    plan_id, _ = held(t2={"spec": "do Stats 2 better"})
    async def odd(conn):  # the API refuses these; older data may hold them
        plan = await store.get_document(conn, plan_id)
        pf = {**plan["body"]["preflight"], "triage": {**verdict("hold"), "answers": {"changes_design": "n/a"}, "at": "2026-11-01T10:00:00+00:00"}}
        await store.add_version(conn, indexer, plan_id, {**plan["body"], "preflight": pf})
        await conn.commit()

    run_db(odd)
    assert "changes_design: n/a" in page(client, project, plan_id)


def test_an_old_preflight_without_triage_shows_no_jev_line(client, project, run_db, indexer, held):
    plan_id, _ = held(t2={"spec": "do Stats 2 better"})
    html = page(client, project, plan_id)
    assert "Waiting on you" in html and "jev-latest" not in html
    assert 'id="preflight"' in html
