"""Plans and their tasks. A plan is drafted by an agent with the human, approved only by the
human, and approval turns its tasks into the queue the runner plays."""

import fnmatch

from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from . import rules, store
from .gates import GateConfig


class TaskSpec(BaseModel):
    title: str
    spec: str = Field(description="what to do, concretely enough to be done without asking")
    layers: list[str] = []
    acceptance: list[str] = Field(default=[], description="verifiable criteria, ideally checked by gates")
    depends_on: list[int] = Field(default=[], description="1-based positions of earlier tasks in this plan")


class Turn(BaseModel):
    role: str
    text: str


class PlanBody(BaseModel):
    title: str
    goal: str
    context: str = ""
    scope: list[str] = []
    out_of_scope: list[str] = []
    acceptance: list[str] = []
    risks: list[str] = []
    tasks: list[TaskSpec]
    discussion: list[Turn] = Field(default=[], description="the questions and answers that shaped the plan")


async def draft(
    conn: AsyncConnection, indexer: store.Indexer, project: str, feature: str | None, layers: list[str],
    body: PlanBody, plan_id: int | None = None,
) -> int:
    """Creates a draft plan, or revises one that is still a draft."""
    data = body.model_dump(mode="json")
    async with conn.transaction():
        if plan_id is None:
            return await store.create_document(conn, indexer, project=project, type="plan", body=data,
                                               feature=feature, layers=layers, status="draft")
        doc = await store.get_document(conn, plan_id)
        if not doc or doc["type"] != "plan" or doc["project"] != project:
            raise rules.Forbidden(f"plan {plan_id} does not exist in {project}")
        if doc["status"] != "draft":
            raise rules.Forbidden(f"plan {plan_id} is {doc['status']}; only drafts can be revised")
        await store.add_version(conn, indexer, plan_id, data)
        return plan_id


async def approve(conn: AsyncConnection, indexer: store.Indexer, plan_id: int) -> list[int]:
    """Human-only. Approves the plan and queues its tasks in order."""
    async with conn.transaction():
        plan = await rules.transition(conn, plan_id, "approved", "human")
        body = PlanBody.model_validate(plan["body"])
        gate_doc = await store.latest_of_type(conn, plan["project"], "gate_config")
        held = GateConfig.from_yaml(gate_doc["body"]["yaml"]).fermata.features if gate_doc else []
        fermata = bool(plan["feature"]) and any(fnmatch.fnmatch(plan["feature"], f) for f in held)
        task_ids: list[int] = []
        for seq, t in enumerate(body.tasks, start=1):
            task_id = await store.create_document(
                conn, indexer, project=plan["project"], type="task", status="pending",
                feature=plan["feature"], layers=t.layers or plan["layers"],
                body={"title": t.title, "spec": t.spec, "acceptance": t.acceptance, "plan_id": plan_id,
                      "seq": seq, "depends_on": [task_ids[i - 1] for i in t.depends_on if 0 < i < seq],
                      "fermata": fermata},
            )
            await store.link(conn, plan_id, task_id, "has_task")
            task_ids.append(task_id)
    return task_ids
