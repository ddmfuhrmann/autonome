from datetime import UTC, datetime

import psycopg_pool
import pytest
from starlette.testclient import TestClient

from autonome_context import plans, rules, services, store, web
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
            await store.save_singleton(conn, indexer, project, "gate_config", {"yaml": "review_after_merge: {features: [wallet*]}\n"})
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
    log = page.split('id="live"', 1)[1]
    assert '<time datetime="' in log and 'data-fmt="clock"' in log


def _task_in(run_db, indexer, project, *path):
    async def go(conn):
        plan_id = await plans.draft(conn, indexer, project, "live", [], PlanBody(title="L", goal="g", tasks=[TaskSpec(title="Routes", spec="s")]))
        [task] = await plans.approve(conn, indexer, plan_id)
        for to in path:
            await rules.transition(conn, task, to, "runner")
        await conn.commit()
        return task
    return run_db(go)


def test_a_pending_task_page_refreshes_with_an_empty_live_section(client, project, run_db, indexer):
    task = _task_in(run_db, indexer, project)
    page = client.get(f"/ui/p/{project}/documents/{task}").text
    assert 'data-refresh="3"' in page and 'class="live"' in page
    assert 'id="live"' in page and "Waiting for the session's first step" in page


def test_a_playing_task_page_swaps_the_header_the_live_log_and_the_runs(client, project, run_db, indexer):
    task = _task_in(run_db, indexer, project, "playing")
    page = client.get(f"/ui/p/{project}/documents/{task}").text
    assert 'data-refresh="3"' in page and 'class="live"' in page
    for region in ('id="task-meta" data-live', 'id="live" aria-labelledby="live-h" data-live', 'id="runs" data-live'):
        assert region in page


@pytest.mark.parametrize("path", [("playing", "done"), ("playing", "blocked"), ("playing", "on_hold")])
def test_a_task_that_cannot_change_by_itself_does_not_refresh(client, project, run_db, indexer, path):
    task = _task_in(run_db, indexer, project, *path)
    page = client.get(f"/ui/p/{project}/documents/{task}").text
    assert "data-refresh" not in page.split("<script", 1)[0] and 'class="live"' not in page


def test_two_refresh_requests_return_the_same_regions(client, project, run_db, indexer):
    import re

    task = _task_in(run_db, indexer, project, "playing")
    ids = []
    for _ in range(2):
        r = client.get(f"/ui/p/{project}/documents/{task}", headers={"X-Refresh": "1"})
        assert r.status_code == 200
        ids.append(re.findall(r'id="([^"]+)"[^>]*data-live', r.text))
    assert ids[0] == ids[1] and {"task-meta", "live", "runs"} <= set(ids[0])


def test_the_refresh_script_stops_when_done_fails_on_bad_responses_and_skips_only_form_fields():
    from pathlib import Path

    base = (Path(web.__file__).parent / "templates" / "base.html").read_text()
    assert 'next.querySelector("[data-refresh]")' in base and "clearInterval(poll)" in base
    assert "if (!res.ok) throw" in base
    assert 'const field = "input, textarea, select, [contenteditable]"' in base and "active.matches(field)" in base
    assert "el.contains(document.activeElement)" not in base


def test_base_defines_the_time_formatter_and_the_tz_cookie():
    from pathlib import Path

    base = (Path(web.__file__).parent / "templates" / "base.html").read_text()
    assert "function formatTimes(root)" in base
    assert "time[data-fmt]" in base and "document.cookie = `tz=${tz}; path=/; max-age=31536000; samesite=lax`" in base


@pytest.mark.parametrize("path", [
    "", "/setlist", "/approvals", "/documents", "/features", "/waves", "/insights",
    "/conventions", "/config", "/search", "/lab", "/triage", "/features/links",
])
def test_every_page_carries_the_time_formatter_and_the_tz_cookie(client, project, path):
    res = client.get(f"/ui/p/{project}{path}", follow_redirects=True)
    assert res.status_code == 200
    assert "formatTimes(" in res.text and "tz=${tz}" in res.text


def test_a_task_page_carries_the_time_formatter_and_the_tz_cookie(client, project, blocked_task):
    page = client.get(f"/ui/p/{project}/documents/{blocked_task}").text
    assert "formatTimes(" in page and "tz=${tz}" in page


def test_the_refresh_formats_times_in_the_swapped_region_only():
    from pathlib import Path

    base = (Path(web.__file__).parent / "templates" / "base.html").read_text()
    assert "el.replaceWith(fresh); formatTimes(fresh);" in base


@pytest.mark.parametrize("cookie", ["Mars/Olympus", "../../etc/passwd", "<script>", "A" * 300, ""])
def test_the_documents_page_survives_a_bad_tz_cookie(client, project, cookie):
    client.cookies.set("tz", cookie)
    assert client.get(f"/ui/p/{project}/documents").status_code == 200


def test_the_documents_page_groups_days_in_the_zone_of_the_tz_cookie(client, project, run_db, indexer, monkeypatch):
    from conftest import make_handoff

    async def go(conn):
        await store.put_handoff(conn, indexer, make_handoff(project, feature="links", summary="Near midnight."), None)
        await conn.execute("update document set created_at = %s where project_id = (select id from project where slug = %s)",
                           (datetime(2026, 11, 2, 2, 30, tzinfo=UTC), project))
        await conn.commit()
    run_db(go)
    real = web.day_groups
    monkeypatch.setattr(web, "day_groups", lambda docs, now=None, tz=UTC: real(
        docs, now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC), tz=tz))
    url = f"/ui/p/{project}/documents"

    assert ">Today<" in client.get(url).text  # no cookie: UTC, still the 2nd
    client.cookies.set("tz", "America/Sao_Paulo")
    page = client.get(url).text
    assert ">Yesterday<" in page and ">Today<" not in page  # 23:30 on the 1st in São Paulo


def test_the_state_snapshot_lists_tasks_plans_and_what_waits(client, project, blocked_task):
    s = client.get(f"/api/projects/{project}/state").json()
    assert [(t["id"], t["status"]) for t in s["tasks"]] == [(blocked_task, "blocked")]
    assert [w["kind"] for w in s["waiting"]] == ["blocked"] and s["waiting"][0]["blocks"] is True
    assert s["plans"][0]["status"] == "approved" and s["waves"] == []


def test_what_a_merge_left_open_waits_for_review_instead_of_vanishing(client, project, run_db, indexer):
    async def go(conn):
        plan_id = await plans.draft(conn, indexer, project, "links", [], PlanBody(
            title="Links", goal="g", tasks=[TaskSpec(title="Probe", spec="s"), TaskSpec(title="Clean", spec="s")]))
        tasks = await plans.approve(conn, indexer, plan_id)
        await conn.commit()
        return tasks
    probe, clean = run_db(go)
    finding = {"kind": "diverges", "severity": "medium", "issue": "The probe is   scanned in every context."}
    for t, findings in ((probe, [finding, {"kind": "missing", "severity": "low", "issue": "nit"}]), (clean, [])):
        client.post(f"/api/tasks/{t}/transition", json={"to": "playing"})
        client.post(f"/api/tasks/{t}/transition", json={"to": "done", "run": {
            "outcome": "merged", "merged_sha": "a3f9c1e0", "review": {"verdict": "pass", "findings": findings}}})
    handoff = {"project": project, "session_id": "s-9", "summary": "done",
               "plan_adherence": [{"item": "trace in Grafana", "status": "partial", "note": "stack not run"},
                                  {"item": "test passes", "status": "done"}]}
    assert client.post("/api/handoffs", json={"handoff": handoff, "task_id": clean}).status_code == 201

    async def bodies(conn):
        return {t: (await store.get_document(conn, t))["body"].get("playback_review") for t in (probe, clean)}
    reviews = run_db(bodies)
    assert reviews[probe] == {"status": "pending", "loose_ends": [
        "review, medium diverges: The probe is scanned in every context."]}
    assert reviews[clean]["loose_ends"] == ["handoff, partial: trace in Grafana (stack not run)"]
    review = client.get(f"/ui/p/{project}/approvals").text.split('id="review-h"', 1)[1]
    assert "Left open at merge" in review and "trace in Grafana" in review and "nit" not in review


def test_insights_count_review_rounds_as_encores_and_show_the_debt(client, project, run_db, indexer):
    async def go(conn):
        plan_id = await plans.draft(conn, indexer, project, "links", [], PlanBody(
            title="Links", goal="g", tasks=[TaskSpec(title="Probe", spec="s")]))
        [task] = await plans.approve(conn, indexer, plan_id)
        async with conn.transaction():
            await store.create_document(conn, indexer, project=project, type="convention",
                                        body={"title": "Huge", "rule": "x " * 2500}, status="active")
        await conn.commit()
        return task
    task = run_db(go)
    client.post(f"/api/tasks/{task}/transition", json={"to": "playing"})
    client.post(f"/api/tasks/{task}/transition", json={"to": "done", "run": {
        "outcome": "merged", "review_rounds": 3, "tokens": 1000, "cost_usd": 0.5,
        "review": {"verdict": "pass", "findings": [{"kind": "missing", "severity": "medium", "issue": "weak test"}]}}})

    async def go2(conn):
        from autonome_context import insights
        return await insights.build(conn, project, indexer)
    data = run_db(go2)
    assert data["encores_per_task"]["max"] == 2
    assert data["spent"]["cost_usd"] == 0.5 and data["spent"]["costliest"][0]["review_rounds"] == 3
    assert data["loose_ends"][0]["loose_ends"] == ["review, medium missing: weak test"]
    assert data["heavy_conventions"][0]["title"] == "Huge"


def test_adopting_a_replacing_convention_archives_the_old_one_and_edits_keep_its_why(client, project, run_db, indexer):
    async def go(conn):
        async with conn.transaction():
            old = await store.create_document(conn, indexer, project=project, type="convention",
                                              body={"title": "Old", "rule": "never commit package.json", "why": "w"},
                                              status="active")
            new = await store.create_document(conn, indexer, project=project, type="convention",
                                              body={"title": "New", "rule": "commit package.json", "why": "vaadin",
                                                    "replaces": old}, status="proposed")
        return old, new
    old, new = run_db(go)
    page = client.get(f"/ui/p/{project}/approvals").text
    assert f"Replaces <a href=\"/ui/p/{project}/documents/{old}\">#{old}</a>" in page
    r = client.post(f"/ui/p/{project}/documents/{new}/transition", data={"to": "active"}, headers=FETCH)
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert status(run_db, old) == "archived"

    client.post(f"/ui/p/{project}/conventions/{new}", data={"title": "New", "rule": "commit package.json and the lockfile"})

    async def body(conn):
        return (await store.get_document(conn, new))["body"]
    b = run_db(body)
    assert b["rule"] == "commit package.json and the lockfile" and b["why"] == "vaadin" and b["replaces"] == old


def test_a_draft_plan_shows_its_design_and_full_task_specs(client, project, run_db, indexer):
    async def go(conn):
        plan = await plans.draft(conn, indexer, project, "ledger", [], PlanBody(
            title="Ledger", goal="g", tasks=[TaskSpec(title="Entries", spec="Adds entries.\n\n**Where**\n\n- `ledger.py`",
                                                      acceptance=["amounts are positive"])],
            design=[plans.DesignChoice(topic="Data access", chosen="Repository", why="queries in one place", options=[
                plans.DesignOption(name="Repository", tradeoffs="one more class"),
                plans.DesignOption(name="Inline SQL", tradeoffs="spreads")])]))
        await conn.commit()
        return plan

    page = client.get(f"/ui/p/{project}/documents/{run_db(go)}").text
    assert 'id="design"' in page and "Data access" in page and 'class="is-chosen"><strong>Repository' in page
    assert "<strong>Where</strong>" in page and "<code>ledger.py</code>" in page
    assert "<li>amounts are positive</li>" in page


def test_documents_facets_narrow_the_list_and_the_drawer_shows_the_picked_document(client, project, run_db, indexer):
    from conftest import make_handoff
    from autonome_context.handoff import Decision

    async def go(conn):
        await store.put_handoff(conn, indexer, make_handoff(project, feature="links", layers=["api"], decisions=[
            Decision(title="Kept codes at 7 characters", rationale="62^7 is plenty"),
            Decision(title="Dict instead of a response model", rationale="faster", deviation=True)]), None)
        await store.put_handoff(conn, indexer, make_handoff(project, feature="billing", layers=["domain"],
                                                            summary="Refunds are idempotent."), None)
        await conn.commit()
        return {d["body"].get("title") or d["type"]: d for d in await store.list_documents(conn, project)}
    docs = run_db(go)
    kept, deviation = docs["Kept codes at 7 characters"], docs["Dict instead of a response model"]
    base = f"/ui/p/{project}/documents"

    page = client.get(base).text
    assert "Kept codes at 7 characters" in page and "Refunds are idempotent" in page
    assert 'class="drawer' not in page and ">Today<" in page

    # a facet narrows the list, its own counts stay whole, and other facets count what is left
    page = client.get(base, params={"type": "decision"}).text
    assert "Refunds are idempotent" not in page and "Kept codes at 7 characters" in page
    assert f'href="{base}?type=decision&amp;type=handoff"' in page  # toggling adds, it never replaces
    assert 'aria-current="true"' in page
    page = client.get(base, params=[("feature", "links"), ("feature", "billing")]).text
    assert "Refunds are idempotent" in page and "Kept codes" in page  # values in one facet are OR'd
    assert "Refunds" not in client.get(base, params={"layer": "api"}).text

    # the query matches titles, summaries and refs like D-12
    assert "Kept codes" not in client.get(base, params={"q": "idempotent"}).text
    assert "Kept codes" in client.get(base, params={"q": f"D-{kept['id']}"}).text

    # ?doc= opens the drawer over the list, keeps the filters, and links to prev/next and the full page
    page = client.get(base, params={"type": "decision", "doc": deviation["id"]}).text
    drawer = page.split('class="drawer', 1)[1]
    assert "Dict instead of a response model" in drawer and "Review in Approvals" in drawer  # proposed: decide elsewhere
    assert f'href="/ui/p/{project}/documents/{deviation["id"]}">Open<' in drawer
    assert f'data-close="{base}?type=decision"' in page
    assert "records (from)" in drawer  # the handoff that recorded it
    # changing a filter closes it; a document from another project never opens
    assert f"type=decision&amp;type=handoff\"" in page and "doc=" not in page.split('aria-label="Type"', 1)[1].split("</div>", 1)[0]
    assert 'class="drawer' not in client.get(f"/ui/p/other-{project}/documents", params={"doc": kept["id"]}).text
