import pytest

from autonome_context import briefing, plans, rules, store
from autonome_context.gates import GateConfig
from autonome_context.plans import PlanBody, ProposedGate, Preflight, TaskSpec, WaveBody, WaveFeature


def body(title: str, tasks=1, **kw) -> PlanBody:
    return PlanBody(title=title, goal=f"{title} goal",
                    tasks=[TaskSpec(title=f"{title} {i}", spec=f"do {title} {i}") for i in range(1, tasks + 1)], **kw)


async def play(conn, task_id):
    await rules.transition(conn, task_id, "running", "runner")
    await rules.transition(conn, task_id, "done", "runner")


async def gates_of(conn, project):
    doc = await store.latest_of_type(conn, project, "gate_config")
    return [g.name for g in GateConfig.from_yaml(doc["body"]["yaml"]).gates] if doc else []


async def two_feature_wave(conn, indexer, project):
    links = await plans.draft(conn, indexer, project, "links", ["api"], body("Links"))
    stats = await plans.draft(conn, indexer, project, "stats", ["api"], body("Stats", tasks=2))
    wave = await plans.draft_wave(conn, indexer, project, WaveBody(
        title="MVP", goal="shorten and count", features=[WaveFeature(plan_id=links),
                                                        WaveFeature(plan_id=stats, depends_on=[1])]))
    return links, stats, wave


def test_a_wave_is_approved_at_once_and_plays_features_in_order(run_db, project, indexer):
    async def go(conn):
        links, stats, wave = await two_feature_wave(conn, indexer, project)
        with pytest.raises(rules.Forbidden):
            await plans.approve(conn, indexer, stats)  # only with its wave
        with pytest.raises(rules.Forbidden):
            await rules.transition(conn, wave, "approved", "agent")
        queued = await plans.approve_wave(conn, indexer, wave)
        [links_task] = queued[links]
        first = await store.next_task(conn, project)
        await rules.transition(conn, links_task, "running", "runner")
        while_links_plays = await store.next_task(conn, project)  # stats waits for the links plan
        await rules.transition(conn, links_task, "done", "runner")
        await store.finish_plan_if_complete(conn, links)
        after = await store.next_task(conn, project)
        for t in queued[stats]:
            await play(conn, t)
        await store.finish_plan_if_complete(conn, stats)
        await plans.finish_wave_if_complete(conn, stats)
        return first, links_task, while_links_plays, after, queued[stats], (await store.get_document(conn, wave))

    first, links_task, while_links_plays, after, stats_tasks, wave = run_db(go)
    assert first["id"] == links_task and while_links_plays is None and after["id"] == stats_tasks[0]
    assert wave["status"] == "done"


def test_a_plan_cannot_join_two_waves_and_dependencies_point_back(run_db, project, indexer):
    async def go(conn):
        links, _, _ = await two_feature_wave(conn, indexer, project)
        loose = await plans.draft(conn, indexer, project, "loose", [], body("Loose"))
        errors = []
        for features in ([WaveFeature(plan_id=links)], [WaveFeature(plan_id=loose, depends_on=[1])]):
            try:
                await plans.draft_wave(conn, indexer, project, WaveBody(title="x", goal="y", features=features))
            except rules.Forbidden as e:
                errors.append(str(e))
        return errors

    errors = run_db(go)
    assert "already belongs" in errors[0] and "earlier features" in errors[1]


def test_preflight_ready_adds_notes_to_the_briefing(run_db, project, indexer):
    async def go(conn):
        links, stats, wave = await two_feature_wave(conn, indexer, project)
        queued = await plans.approve_wave(conn, indexer, wave)
        await play(conn, queued[links][0])
        await store.finish_plan_if_complete(conn, links)
        task = await store.get_document(conn, queued[stats][0])
        before = plans.needs_preflight(task, await store.get_document(conn, stats))
        await plans.preflight(conn, indexer, stats, Preflight(verdict="ready", notes="links live in links.py"))
        after = plans.needs_preflight(task, await store.get_document(conn, stats))
        return before, after, await briefing.build(conn, project, task["id"])

    before, after, text = run_db(go)
    assert before and not after and "links live in links.py" in text


def test_the_briefing_reads_the_preflight_notes_from_body_preflight_notes(run_db, project, indexer):
    async def go(conn):
        plan = await plans.draft(conn, indexer, project, "notes", [], body("Notes"))
        [task] = await plans.approve(conn, indexer, plan)
        await plans.preflight(conn, indexer, plan, Preflight(verdict="ready", notes="X-from-preflight"))
        stored = await store.get_document(conn, plan)
        return stored["body"]["preflight"]["notes"], await briefing.build(conn, project, task)

    notes, text = run_db(go)
    assert notes == "X-from-preflight"
    assert "## Preflight notes" in text and "X-from-preflight" in text


def test_a_revised_plan_is_held_until_the_human_accepts_it(run_db, project, indexer):
    async def go(conn):
        links, stats, wave = await two_feature_wave(conn, indexer, project)
        queued = await plans.approve_wave(conn, indexer, wave)
        await play(conn, queued[links][0])
        await store.finish_plan_if_complete(conn, links)
        status = await plans.preflight(conn, indexer, stats, Preflight(
            verdict="revise", notes="the counter belongs in the redirect", plan=body("Stats v2", tasks=3)))
        held_claim = await store.next_task(conn, project)
        await plans.resolve_preflight(conn, indexer, stats, accept=True)
        plan = await store.get_document(conn, stats)
        old = [await store.get_document(conn, t) for t in queued[stats]]
        new = await store.next_task(conn, project)
        return status, held_claim, plan, old, new

    status, held_claim, plan, old, new = run_db(go)
    assert status == "held" and held_claim is None
    assert plan["status"] == "approved" and plan["body"]["title"] == "Stats v2"
    assert plan["body"]["preflight"]["verdict"] == "revised" and len(plan["body"]["tasks"]) == 3
    assert all(t["status"] == "abandoned" for t in old)
    assert new["body"]["title"] == "Stats v2 1" and new["body"]["after_plans"] == [old[0]["body"]["after_plans"][0]]


def test_proposed_gates_join_the_config_on_approval_or_when_their_task_is_merged(run_db, project, indexer):
    async def go(conn):
        async with conn.transaction():
            await store.save_singleton(conn, indexer, project, "gate_config",
                                       {"yaml": "gates:\n  - {name: test, run: pytest}\n"})
        plan_id = await plans.draft(conn, indexer, project, "web", [], body("Web", tasks=2, gates=[
            ProposedGate(name="lint", run="ruff check ."),
            ProposedGate(name="e2e", run="pnpm test", with_task=2),
            ProposedGate(name="test", run="something else")]))
        first, second = await plans.approve(conn, indexer, plan_id)
        on_approval = await gates_of(conn, project)
        task = await store.get_document(conn, second)
        await plans.apply_gates(conn, indexer, project, task["body"]["adds_gates"])
        return on_approval, await gates_of(conn, project)

    on_approval, after_task = run_db(go)
    assert on_approval == ["test", "lint"]  # an existing gate is never replaced
    assert after_task == ["test", "lint", "e2e"]


def test_proposed_conventions_become_active_on_approval_and_never_replace_one(run_db, project, indexer):
    from autonome_context.plans import ProposedConvention

    async def go(conn):
        async with conn.transaction():
            await store.create_document(conn, indexer, project=project, type="convention", status="active",
                                        layers=["api"], body={"title": "Response models", "rule": "the original"})
        plan_id = await plans.draft(conn, indexer, project, "links", ["api"], body("Links", conventions=[
            ProposedConvention(title="Response models", rule="a looser one", layers=["api"]),
            ProposedConvention(title="No raw 500", rule="every expected failure is an HTTPException", layers=["api"]),
            ProposedConvention(title="Foreign keys on", rule="PRAGMA foreign_keys = ON", layers=["persistence"])]))
        summary = plans.plan_summary(await store.get_document(conn, plan_id))
        before = await store.conventions_for(conn, project, ["api", "persistence"])
        await plans.approve(conn, indexer, plan_id)
        after = await store.conventions_for(conn, project, ["api", "persistence"])
        return summary, before, after

    summary, before, after = run_db(go)
    assert "Adds conventions: Response models (api): a looser one" in summary
    assert [c["body"]["title"] for c in before] == ["Response models"]
    assert sorted(c["body"]["title"] for c in after) == ["Foreign keys on", "No raw 500", "Response models"]
    assert next(c for c in after if c["body"]["title"] == "Response models")["body"]["rule"] == "the original"


def test_gates_waiting_for_their_setup_task_are_listed_as_pending(run_db, project, indexer):
    from autonome_context import api

    async def go(conn):
        plan_id = await plans.draft(conn, indexer, project, "links", [], body("Links", tasks=2, gates=[
            ProposedGate(name="ruff-format", run="uv run ruff format --check .", with_task=1),
            ProposedGate(name="pytest", run="uv run pytest -q", with_task=1)]))
        first, _ = await plans.approve(conn, indexer, plan_id)
        before = await api.gate_config_body(conn, project)
        await play(conn, first)
        await plans.apply_gates(conn, indexer, project, (await store.get_document(conn, first))["body"]["adds_gates"])
        after = await api.gate_config_body(conn, project)
        return first, before, after

    first, before, after = run_db(go)
    assert before["version"] is None and before["config"]["gates"] == []
    assert [(g["name"], g["task_id"]) for g in before["pending"]] == [("ruff-format", first), ("pytest", first)]
    assert [g["name"] for g in after["config"]["gates"]] == ["ruff-format", "pytest"] and after["pending"] == []


def test_a_plan_keeps_the_backlog_items_it_delivers_in_step(run_db, project, indexer):
    from autonome_context import backlog, web
    async def setup(conn):
        async with conn.transaction():
            await store.create_document(conn, indexer, project=project, type="backlog", status="open", body={
                "wave": 2, "title": "Wave 2", "items": [{"id": "w2-ledger", "title": "Ledger", "status": "next"},
                                                         {"id": "w2-audit", "title": "Audit", "status": "next"}]})
        with pytest.raises(rules.Forbidden, match="w2-ledger"):
            await plans.draft(conn, indexer, project, "ledger", [], PlanBody(
                title="Ledger", goal="g", tasks=[TaskSpec(title="t", spec="s")], backlog_items=["w2-ledgr"]))
        done_plan = await plans.draft(conn, indexer, project, "ledger", [], PlanBody(
            title="Ledger", goal="g", tasks=[TaskSpec(title="t", spec="s")], backlog_items=["w2-ledger"]))
        dropped = await plans.draft(conn, indexer, project, "audit", [], PlanBody(
            title="Audit", goal="g", tasks=[TaskSpec(title="t", spec="s")], backlog_items=["w2-audit"]))
        [task] = await plans.approve(conn, indexer, done_plan)
        await plans.approve(conn, indexer, dropped)
        await conn.commit()
        return done_plan, dropped, task

    done_plan, dropped, task = run_db(setup)

    async def status(conn):
        return {i["id"]: i["status"] for w in await backlog.list_backlog(conn, project) for i in w["items"]}
    assert run_db(status) == {"w2-ledger": "doing", "w2-audit": "doing"}

    async def finish(conn):
        await rules.transition(conn, task, "running", "runner")
        await rules.transition(conn, task, "done", "runner")
        assert await store.finish_plan_if_complete(conn, done_plan)
        await plans.finish_wave_if_complete(conn, done_plan)
        await web.decide(conn, indexer, await store.get_document(conn, dropped), "abandoned")
        await conn.commit()
        return await backlog.item_history(conn, project, "w2-ledger")
    history = run_db(finish)
    assert run_db(status) == {"w2-ledger": "done", "w2-audit": "next"}
    assert history[-1]["by"] == f"plan #{done_plan}" and history[-1]["new"] == "done"


def test_a_design_choice_must_pick_one_of_its_options():
    from autonome_context.plans import DesignChoice, DesignOption
    options = [DesignOption(name="repository", tradeoffs="one place for queries"),
               DesignOption(name="queries in the service", tradeoffs="fewer files")]
    assert DesignChoice(topic="data access", options=options, chosen="repository", why="w").chosen == "repository"
    with pytest.raises(ValueError, match="not one of the options"):
        DesignChoice(topic="data access", options=options, chosen="active record", why="w")
    with pytest.raises(ValueError):
        DesignChoice(topic="data access", options=options[:1], chosen="repository", why="w")


def test_the_design_reaches_the_briefing_the_summary_and_survives_a_revision(run_db, project, indexer):
    from autonome_context.plans import DesignChoice, DesignOption
    design = [DesignChoice(topic="data access", chosen="repository", why="queries in one place", options=[
        DesignOption(name="repository", tradeoffs="one more class"), DesignOption(name="inline SQL", tradeoffs="spreads")])]

    async def go(conn):
        links, stats, wave = await two_feature_wave(conn, indexer, project)
        await plans.draft(conn, indexer, project, "stats", [], body("Stats", design=design), plan_id=stats)
        summary = plans.plan_summary(await store.get_document(conn, stats))
        queued = await plans.approve_wave(conn, indexer, wave)
        text = await briefing.build(conn, project, queued[stats][0])
        await play(conn, queued[links][0])
        await store.finish_plan_if_complete(conn, links)
        await plans.preflight(conn, indexer, stats, Preflight(verdict="revise", notes="n", plan=body("Stats v2")))
        await plans.resolve_preflight(conn, indexer, stats, accept=True)
        return summary, text, (await store.get_document(conn, stats))["body"]

    summary, text, revised = run_db(go)
    assert "Design: data access: repository (over inline SQL)" in summary
    assert "## Design the plan chose" in text and "data access: repository. queries in one place" in text
    assert revised["title"] == "Stats v2" and revised["design"][0]["chosen"] == "repository"
