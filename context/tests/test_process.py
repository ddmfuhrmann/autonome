import pytest

from autonome_context import briefing, plans, rules, store
from autonome_context.plans import PlanBody, TaskSpec


def plan_body(**kw) -> PlanBody:
    base = dict(title="Signup", goal="Self-service signup", out_of_scope=["billing"],
                tasks=[TaskSpec(title="Form", spec="Build the form", layers=["ui"], acceptance=["form renders"]),
                       TaskSpec(title="API", spec="POST /accounts", layers=["api"], depends_on=[1])])
    base.update(kw)
    return PlanBody(**base)


def test_agents_cannot_pass_human_gates():
    assert not rules.allowed("plan", "draft", "approved", "agent")
    assert rules.allowed("plan", "draft", "approved", "human")
    assert not rules.allowed("task", "on_hold", "released", "runner")
    assert not rules.allowed("plan", "done", "draft", "human")  # undefined transitions are refused


def test_draft_revise_approve_queues_tasks_with_fermata(run_db, project, indexer):
    async def go(conn):
        async with conn.transaction():
            await store.create_document(conn, indexer, project=project, type="gate_config", status="active",
                                        body={"yaml": "fermata: {features: [sign*]}"})
        plan_id = await plans.draft(conn, indexer, project, "signup", ["ui", "api"], plan_body())
        await plans.draft(conn, indexer, project, "signup", ["ui", "api"], plan_body(goal="Signup v2"), plan_id)
        with pytest.raises(rules.Forbidden):
            await rules.transition(conn, plan_id, "approved", "agent")
        task_ids = await plans.approve(conn, indexer, plan_id)
        with pytest.raises(rules.Forbidden):
            await plans.draft(conn, indexer, project, "signup", [], plan_body(), plan_id)
        tasks = [await store.get_document(conn, t) for t in task_ids]
        plan = await store.get_document(conn, plan_id)
        return plan, tasks

    plan, tasks = run_db(go)
    assert plan["status"] == "approved" and plan["version"] == 2 and plan["body"]["goal"] == "Signup v2"
    assert [t["status"] for t in tasks] == ["pending", "pending"]
    assert tasks[0]["layers"] == ["ui"] and tasks[1]["body"]["depends_on"] == [tasks[0]["id"]]
    assert all(t["body"]["fermata"] for t in tasks)


def test_task_briefing_has_spec_out_of_scope_and_layer_conventions(run_db, project, indexer):
    async def go(conn):
        async with conn.transaction():
            for title, layers in [("UI rules", ["ui"]), ("API rules", ["api"]), ("Everywhere", [])]:
                await store.create_document(conn, indexer, project=project, type="convention", status="active",
                                            body={"title": title, "rule": f"{title} body"}, layers=layers)
        plan_id = await plans.draft(conn, indexer, project, "signup", [], plan_body())
        [ui_task, _] = await plans.approve(conn, indexer, plan_id)
        return await briefing.build(conn, project, ui_task), await briefing.build(conn, project)

    task_text, interactive = run_db(go)
    assert "Build the form" in task_text and "billing" in task_text
    assert "UI rules body" in task_text and "Everywhere body" in task_text and "API rules" not in task_text
    assert "Conventions:" in interactive
