import pytest

from autonome_context import briefing, plans, rules, store
from autonome_context.gates import GateConfig
from autonome_context.plans import PlanBody, ProposedGate, Rehearsal, TaskSpec, WaveBody, WaveFeature


def body(title: str, tasks=1, **kw) -> PlanBody:
    return PlanBody(title=title, goal=f"{title} goal",
                    tasks=[TaskSpec(title=f"{title} {i}", spec=f"do {title} {i}") for i in range(1, tasks + 1)], **kw)


async def play(conn, task_id):
    await rules.transition(conn, task_id, "playing", "runner")
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
        await rules.transition(conn, links_task, "playing", "runner")
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


def test_rehearsal_ready_adds_notes_to_the_briefing(run_db, project, indexer):
    async def go(conn):
        links, stats, wave = await two_feature_wave(conn, indexer, project)
        queued = await plans.approve_wave(conn, indexer, wave)
        await play(conn, queued[links][0])
        await store.finish_plan_if_complete(conn, links)
        task = await store.get_document(conn, queued[stats][0])
        before = plans.needs_rehearsal(task, await store.get_document(conn, stats))
        await plans.rehearse(conn, indexer, stats, Rehearsal(verdict="ready", notes="links live in links.py"))
        after = plans.needs_rehearsal(task, await store.get_document(conn, stats))
        return before, after, await briefing.build(conn, project, task["id"])

    before, after, text = run_db(go)
    assert before and not after and "links live in links.py" in text


def test_a_revised_plan_is_held_until_the_human_accepts_it(run_db, project, indexer):
    async def go(conn):
        links, stats, wave = await two_feature_wave(conn, indexer, project)
        queued = await plans.approve_wave(conn, indexer, wave)
        await play(conn, queued[links][0])
        await store.finish_plan_if_complete(conn, links)
        status = await plans.rehearse(conn, indexer, stats, Rehearsal(
            verdict="revise", notes="the counter belongs in the redirect", plan=body("Stats v2", tasks=3)))
        held_claim = await store.next_task(conn, project)
        await plans.resolve_rehearsal(conn, indexer, stats, accept=True)
        plan = await store.get_document(conn, stats)
        old = [await store.get_document(conn, t) for t in queued[stats]]
        new = await store.next_task(conn, project)
        return status, held_claim, plan, old, new

    status, held_claim, plan, old, new = run_db(go)
    assert status == "held" and held_claim is None
    assert plan["status"] == "approved" and plan["body"]["title"] == "Stats v2"
    assert plan["body"]["rehearsal"]["verdict"] == "revised" and len(plan["body"]["tasks"]) == 3
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
