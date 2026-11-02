"""Pausing and resuming from the MCP tools and the UI."""

import pytest

from autonome_context import rules, store

from test_approval import call, server  # noqa: F401  (the MCP server fixture and client)
from test_pause import pending_task  # noqa: F401  (a project with one pending task)
from test_web import FETCH, client, status  # noqa: F401  (the client fixture)


@pytest.fixture
def paused_task(project, run_db, pending_task):
    task = pending_task(project)
    run_db(lambda c: rules.transition(c, task, "running", "runner"))
    run_db(lambda c: rules.transition(c, task, "paused", "runner"))
    return task


def pause_state(run_db, project):
    return run_db(lambda c: store.project_pause(c, project))


def test_the_agent_pauses_the_queue_with_a_reason(server, project, run_db, pending_task):
    pending_task(project)
    result, _ = call(server, "queue_pause", {"project": project, "reason": "wrong branch"}, None)
    assert result["paused"]["by"] == "agent:mcp" and result["paused"]["reason"] == "wrong branch"
    assert pause_state(run_db, project)["by"] == "agent:mcp"


@pytest.mark.parametrize("answer, human", [(None, "unavailable"), ("decline", "declined"), ("cancel", "declined")])
def test_without_a_yes_from_the_human_the_queue_stays_paused(server, project, run_db, paused_task, answer, human):
    run_db(lambda c: store.pause_project(c, project, "agent:mcp", "stop"))
    result, _ = call(server, "queue_resume", {"project": project}, answer)
    assert result["status"] == "paused" and result["human"] == human and "/queue" in result["resume_at"]
    assert pause_state(run_db, project) and status(run_db, paused_task) == "paused"


def test_the_human_resumes_in_the_session_and_paused_tasks_go_back_to_pending(server, project, run_db, paused_task):
    run_db(lambda c: store.pause_project(c, project, "agent:mcp", "stop"))
    result, asked = call(server, "queue_resume", {"project": project}, "accept")
    assert result["status"] == "running" and result["resumed_tasks"] == [paused_task]
    assert "agent:mcp" in asked[0] and "stop" in asked[0]
    assert pause_state(run_db, project) is None and status(run_db, paused_task) == "pending"


def test_resuming_a_queue_that_is_not_paused_asks_nobody(server, project, pending_task):
    pending_task(project)
    result, asked = call(server, "queue_resume", {"project": project}, "accept")
    assert result["status"] == "running" and asked == []


def test_a_paused_queue_shows_who_when_and_why_with_resume(client, project, run_db, paused_task):
    run_db(lambda c: store.pause_project(c, project, "agent:mcp", "wrong branch"))
    page = client.get(f"/ui/p/{project}/queue").text
    assert "Queue paused" in page and "agent:mcp" in page and "wrong branch" in page
    assert '<time datetime="' in page
    assert f'action="/ui/p/{project}/queue/resume"' in page and f'action="/ui/p/{project}/queue/pause"' not in page


def test_a_running_queue_shows_pause_with_a_reason_field(client, project, pending_task):
    pending_task(project)
    page = client.get(f"/ui/p/{project}/queue").text
    assert f'action="/ui/p/{project}/queue/pause"' in page and 'name="reason"' in page
    assert f'action="/ui/p/{project}/queue/resume"' not in page


def test_pausing_in_the_ui_records_the_human_and_the_reason(client, project, run_db, pending_task):
    pending_task(project)
    r = client.post(f"/ui/p/{project}/queue/pause", data={"reason": "lunch"}, follow_redirects=False)
    assert r.status_code == 303
    assert pause_state(run_db, project)["by"] == "human:ui" and pause_state(run_db, project)["reason"] == "lunch"


def test_resuming_in_the_ui_puts_paused_tasks_back_to_pending(client, project, run_db, paused_task):
    run_db(lambda c: store.pause_project(c, project, "human:ui", None))
    r = client.post(f"/ui/p/{project}/queue/resume", follow_redirects=False)
    assert r.status_code == 303
    assert pause_state(run_db, project) is None and status(run_db, paused_task) == "pending"


def test_a_paused_task_page_says_paused_and_offers_abandon(client, project, run_db, paused_task):
    page = client.get(f"/ui/p/{project}/documents/{paused_task}").text
    assert "Paused" in page and "Abandon" in page


def test_abandoning_a_paused_task_in_the_ui(client, project, run_db, paused_task):
    r = client.post(f"/ui/p/{project}/documents/{paused_task}/transition", data={"to": "abandoned"}, headers=FETCH)
    assert r.status_code == 200 and r.json()["status"] == "abandoned"
    assert status(run_db, paused_task) == "abandoned"


def test_a_paused_task_waits_on_the_human_in_approvals_with_resume_and_abandon(client, project, run_db, paused_task):
    run_db(lambda c: store.pause_project(c, project, "agent:mcp", "stop"))
    page = client.get(f"/ui/p/{project}/approvals").text
    assert "Paused tasks" in page and f'action="/ui/p/{project}/queue/resume"' in page and "Abandon" in page
