import psycopg_pool
import pytest
from starlette.testclient import TestClient

from autonome_context import plans, rules, services, store
from autonome_context.plans import PlanBody, TaskSpec
from autonome_context.server import build_app

FETCH = {"X-Requested-With": "fetch"}


@pytest.fixture
def client(db_url, indexer, monkeypatch):
    async def get():
        pool = psycopg_pool.AsyncConnectionPool(db_url, open=False, min_size=1, max_size=2)
        await pool.open()
        return pool, indexer

    monkeypatch.setattr(services, "get", get)
    with TestClient(build_app(), base_url="http://127.0.0.1:8765") as c:
        yield c


@pytest.fixture
def blocked_task(run_db, project, indexer):
    async def go(conn):
        plan_id = await plans.draft(conn, indexer, project, "links", [], PlanBody(
            title="Links", goal="g", tasks=[TaskSpec(title="t", spec="s")]))
        [task] = await plans.approve(conn, indexer, plan_id)
        await rules.transition(conn, task, "playing", "runner")
        await rules.transition(conn, task, "blocked", "runner")
        await conn.commit()
        return task

    return run_db(go)


def status(run_db, doc_id):
    async def go(conn):
        return (await store.get_document(conn, doc_id))["status"]
    return run_db(go)


def test_a_retry_answers_with_the_confirmed_status(client, project, blocked_task, run_db):
    r = client.post(f"/ui/p/{project}/documents/{blocked_task}/transition", data={"to": "pending"}, headers=FETCH)
    assert r.status_code == 200 and r.json()["status"] == "pending" and "is now pending" in r.json()["notice"]
    assert status(run_db, blocked_task) == "pending"
    page = client.get(f"/ui/p/{project}/documents/{blocked_task}", params={"done": r.json()["notice"]})
    assert "notice--ok" in page.text


def test_a_refused_or_failed_transition_is_reported_and_changes_nothing(client, project, blocked_task, run_db, monkeypatch):
    r = client.post(f"/ui/p/{project}/documents/{blocked_task}/transition", data={"to": "done"}, headers=FETCH)
    assert r.status_code == 409 and "human may not move" in r.json()["error"]

    async def down(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(rules, "transition", down)
    r = client.post(f"/ui/p/{project}/documents/{blocked_task}/transition", data={"to": "pending"}, headers=FETCH)
    assert r.status_code == 503 and "could not save it" in r.json()["error"]
    assert status(run_db, blocked_task) == "blocked"


def test_the_wave_page_shows_how_each_task_played(client, project, run_db, indexer):
    async def go(conn):
        a = await plans.draft(conn, indexer, project, "links", [], PlanBody(title="Links", goal="g", tasks=[
            TaskSpec(title="Schema", spec="s"), TaskSpec(title="Routes", spec="s")]))
        wave = await plans.draft_wave(conn, indexer, project, plans.WaveBody(
            title="MVP", goal="g", features=[plans.WaveFeature(plan_id=a)]))
        first, second = (await plans.approve_wave(conn, indexer, wave))[a]
        for t, to, run in [(first, "done", {"outcome": "merged", "duration_s": 300, "tokens": 40000, "review_rounds": 1}),
                           (second, "blocked", {"outcome": "blocked", "duration_s": 61, "tokens": 9000, "review_rounds": 3,
                                                "reason": "review: still blocking"})]:
            await rules.transition(conn, t, "playing", "runner")
            await rules.transition(conn, t, to, "runner")
            doc = await store.get_document(conn, t)
            await store.add_version(conn, None, t, {**doc["body"], "runs": [run]})
        await conn.commit()
        return wave

    wave = run_db(go)
    page = client.get(f"/ui/p/{project}/documents/{wave}").text
    assert "How it played" in page and "1 merged · 1 blocked · 0 held · 6m 01s" in page
    assert "review: still blocking" in page and "5m 00s" in page


def test_one_line_markdown_renders_in_lists_and_escapes_html(client, project, run_db, indexer):
    async def go(conn):
        async with conn.transaction():
            await store.save_feature_doc(conn, indexer, project, "links", {
                "overview": "Short links.", "interfaces": [
                    "`uv run uvicorn shortlink.app:app --reload`: starts the API on http://127.0.0.1:8000",
                    "<script>alert(1)</script> is shown, not run"]})
    run_db(go)
    page = client.get(f"/ui/p/{project}/features/links").text
    assert "<code>uv run uvicorn shortlink.app:app --reload</code>: starts the API" in page
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page


def test_tooling_features_and_development_notes_stay_apart_from_the_product(client, project, run_db, indexer):
    async def go(conn):
        async with conn.transaction():
            for slug, kind in [("links", "product"), ("quality-gates", "tooling")]:
                await store.create_document(conn, indexer, project=project, type="task", status="done",
                                            feature=slug, body={"title": "t", "spec": "s"})
                await store.save_feature_doc(conn, indexer, project, slug, {"kind": kind, "overview": f"{slug} doc"})
            await store.save_feature_doc(conn, indexer, project, None, {
                "overview": "A URL shortener.", "development": "Gates: `uv run pytest` runs the tests."})
    run_db(go)
    page = client.get(f"/ui/p/{project}/features").text
    product, dev = page.split("<h2>Development</h2>", 1)
    assert "links doc" in product and "quality-gates doc" not in product
    assert "quality-gates doc" in dev
    assert 'id="dev-h">Development</h2>' in page and "<code>uv run pytest</code> runs the tests" in page


def test_finished_waves_are_listed_until_tuned(client, project, run_db, indexer):
    async def go(conn):
        a = await plans.draft(conn, indexer, project, "links", [], PlanBody(title="L", goal="g", tasks=[TaskSpec(title="t", spec="s")]))
        wave = await plans.draft_wave(conn, indexer, project, plans.WaveBody(title="MVP", goal="g", features=[plans.WaveFeature(plan_id=a)]))
        await plans.approve_wave(conn, indexer, wave)
        await rules.transition(conn, wave, "done", "runner")
        await conn.commit()
        return wave
    wave = run_db(go)
    assert [w["id"] for w in client.get(f"/api/projects/{project}/waves/untuned").json()] == [wave]
    assert client.post(f"/api/waves/{wave}/tuned", json={"summary": "proposed C-1"}).json() == {"ok": True}
    assert client.get(f"/api/projects/{project}/waves/untuned").json() == []
    assert "What the process learned" in client.get(f"/ui/p/{project}/documents/{wave}").text


def test_a_decision_without_a_target_is_refused_clearly(client, project, blocked_task, run_db):
    r = client.post(f"/ui/p/{project}/documents/{blocked_task}/transition", data={}, headers=FETCH)
    assert r.status_code == 400 and "no target status" in r.json()["error"]
    assert status(run_db, blocked_task) == "blocked"


def test_only_what_blocks_the_setlist_counts_as_waiting_on_you(client, project, blocked_task, run_db, indexer):
    from conftest import make_handoff
    from autonome_context.handoff import Decision

    async def go(conn):
        await store.put_handoff(conn, indexer, make_handoff(project, decisions=[
            Decision(title="Kept codes at 7 characters", rationale="62^7 is plenty"),
            Decision(title="Skipped the response model for /health", rationale="plain dict", deviation=True)]), None)
        await conn.commit()
    run_db(go)
    page = client.get(f"/ui/p/{project}/approvals").text
    main, review = page.split('id="review-h"', 1)
    assert "side__count--attn\">1<" in page  # the blocked task only
    assert "Skipped the response model" not in main and "Skipped the response model" in review
    assert "Kept codes at 7 characters" not in page  # in effect, nothing to review


def test_a_choice_within_the_plan_is_in_effect_and_a_rejected_deviation_queues_its_undo(client, project, run_db, indexer):
    from conftest import make_handoff
    from autonome_context.handoff import Decision

    async def go(conn):
        await store.put_handoff(conn, indexer, make_handoff(project, feature="links", decisions=[
            Decision(title="Kept codes at 7 characters", rationale="62^7 is plenty"),
            Decision(title="Dict instead of a response model", rationale="faster", deviation=True)]), None)
        await conn.commit()
        return {d["body"]["title"]: d for d in await store.list_documents(conn, project, type="decision")}
    decisions = run_db(go)
    assert decisions["Kept codes at 7 characters"]["status"] == "accepted"
    deviation = decisions["Dict instead of a response model"]
    assert deviation["status"] == "proposed"
    r = client.post(f"/ui/p/{project}/documents/{deviation['id']}/transition", data={"to": "rejected"}, headers=FETCH)
    assert r.status_code == 200 and r.json()["status"] == "rejected"

    async def tasks(conn):
        return await store.list_documents(conn, project, type="task")
    [undo] = run_db(tasks)
    assert undo["body"]["title"] == "Undo: Dict instead of a response model"
    assert undo["body"]["source"] == f"decision:{deviation['id']}" and undo["feature"] == "links"


def test_playback_merges_then_waits_to_be_heard_and_a_fix_is_a_new_task(client, project, run_db, indexer):
    async def go(conn):
        async with conn.transaction():
            await store.save_singleton(conn, indexer, project, "gate_config", {"yaml": "playback: {features: [wallet*]}\n"})
        plan_id = await plans.draft(conn, indexer, project, "wallet", ["domain"], PlanBody(
            title="Wallet", goal="g", tasks=[TaskSpec(title="Debit", spec="debit"), TaskSpec(title="Credit", spec="credit")]))
        first, second = await plans.approve(conn, indexer, plan_id)
        await conn.commit()
        return first, second
    first, second = run_db(go)
    for t in (first, second):
        client.post(f"/api/tasks/{t}/transition", json={"to": "playing"})
        r = client.post(f"/api/tasks/{t}/transition", json={"to": "done", "run": {"outcome": "merged", "merged_sha": "a3f9c1e0",
                                                                                  "diff": "diff --git a/x b/x\n+1"}})
        assert r.status_code == 200
    page = client.get(f"/ui/p/{project}/approvals").text
    main, review = page.split('id="review-h"', 1)
    assert "Debit" not in main and "Sounds right" in review and "a3f9c1e" in review
    assert 'title="merged, review when you like">2<' in page and "side__count--attn" not in page

    r = client.post(f"/ui/p/{project}/documents/{first}/playback", data={"verdict": "heard"}, headers=FETCH)
    assert r.status_code == 200 and "sounds right" in r.json()["notice"]
    r = client.post(f"/ui/p/{project}/documents/{second}/playback", data={"verdict": "fix", "reason": ""}, headers=FETCH)
    assert r.status_code == 409 and "say what needs fixing" in r.json()["error"]
    r = client.post(f"/ui/p/{project}/documents/{second}/playback",
                    data={"verdict": "fix", "reason": "credit must reject negative amounts"}, headers=FETCH)
    assert r.status_code == 200 and "fix queued" in r.json()["notice"]

    async def after(conn):
        return {t["body"]["title"]: t for t in await store.list_documents(conn, project, type="task")}
    tasks = run_db(after)
    assert tasks["Debit"]["body"]["playback_review"]["status"] == "heard"
    fix = tasks["Fix: Credit"]
    assert "credit must reject negative amounts" in fix["body"]["spec"] and fix["body"]["playback"] is True
    assert tasks["Credit"]["body"]["playback_review"] == {**tasks["Credit"]["body"]["playback_review"], "status": "fix", "fix_task_id": fix["id"]}
    assert "Needs a fix" in client.get(f"/ui/p/{project}/documents/{second}").text


def test_a_playing_task_shows_what_its_session_is_doing(client, project, run_db, indexer, monkeypatch):
    from autonome_context import store as store_mod

    async def go(conn):
        plan_id = await plans.draft(conn, indexer, project, "links", [], PlanBody(title="L", goal="g", tasks=[TaskSpec(title="Routes", spec="s")]))
        [task] = await plans.approve(conn, indexer, plan_id)
        await rules.transition(conn, task, "playing", "runner")
        await conn.commit()
        return task
    task = run_db(go)
    monkeypatch.setattr(store_mod, "EVENTS_PER_TASK", 3)
    r = client.post("/api/events", json={"project": project, "events": [
        {"task_id": task, "role": "maestro", "kind": "stage", "text": "playing on autonome/task-1"},
        {"task_id": task, "role": "musician", "kind": "say", "text": "Customer 123.456.789-09 is a fixture"},
        {"task_id": task, "role": "musician", "kind": "tool", "text": "Bash: uv run pytest -q"},
        {"task_id": task, "role": "musician", "kind": "error", "text": "1 failed"}]})
    assert r.status_code == 201
    events = client.get(f"/api/tasks/{task}/events").json()
    assert [e["kind"] for e in events] == ["say", "tool", "error"]  # pruned to the newest 3
    assert "123.456.789-09" not in events[0]["text"]  # redacted like everything else
    setlist = client.get(f"/ui/p/{project}/setlist").text
    assert "now__doing" in setlist and "1 failed" in setlist
    page = client.get(f"/ui/p/{project}/documents/{task}").text
    assert 'id="live"' in page and 'data-refresh="3"' in page and "Bash: uv run pytest -q" in page
