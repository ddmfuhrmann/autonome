import pytest

from autonome_context import store

from conftest import add_feature
from test_web import client  # noqa: F401  (the client fixture)


@pytest.fixture
def project(project, run_db):
    """The project exists (a feature in the catalog creates it) but the runner has not reported."""
    async def create(conn):
        await add_feature(conn, project, "links")
        await conn.commit()

    run_db(create)
    return project


def report(client, project, body):
    return client.post(f"/api/projects/{project}/runner-status", json=body)


def status(run_db, project):
    return run_db(lambda conn: store.runner_status(conn, project))


def test_nothing_is_known_before_the_runner_reports(run_db, project):
    assert status(run_db, project) == {"triage_key": None, "seen_at": None}


def test_a_report_is_stored_with_the_time(client, run_db, project):
    r = report(client, project, {"triage_key": False})
    assert r.status_code == 200 and r.json() == {"ok": True}
    s = status(run_db, project)
    assert s["triage_key"] is False and s["seen_at"] is not None
    report(client, project, {"triage_key": True})
    assert status(run_db, project)["triage_key"] is True


@pytest.mark.parametrize("body", [
    {"triage_key": True, "key": "sk-secret"},
    {"triage_key": "yes"},
    {"triage_key": 1},
    {"triage_key": None},
    {},
])
def test_a_body_that_is_not_just_a_boolean_is_a_422_and_stores_nothing(client, run_db, project, body):
    r = report(client, project, body)
    assert r.status_code == 422
    assert "sk-secret" not in r.text
    assert status(run_db, project) == {"triage_key": None, "seen_at": None}


def test_an_unknown_project_is_a_404(client):
    r = report(client, "no-such-project", {"triage_key": True})
    assert r.status_code == 404 and isinstance(r.json()["error"], str)
