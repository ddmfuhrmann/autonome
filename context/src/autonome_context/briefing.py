"""What an agent should know when a session starts: injected by the SessionStart hook."""

from psycopg import AsyncConnection

from . import store
from .gates import GateConfig

_MAX_CHARS = 14_000


def _list(items) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- (none)"


async def build(conn: AsyncConnection, project: str, task_id: int | None = None) -> str:
    if task_id is None:
        return await _interactive(conn, project)
    task = await store.get_document(conn, task_id)
    if not task or task["type"] != "task" or task["project"] != project:
        return f"autonome: task {task_id} not found in {project}."
    t = task["body"]
    plan = await store.get_document(conn, t["plan_id"])
    p = plan["body"] if plan else {}
    parts = [
        f"# autonome task #{task_id}: {t['title']}",
        f"Project {project}, feature {task['feature'] or '-'}, layers {', '.join(task['layers']) or '-'}.",
        f"## What to do\n{t['spec']}",
        f"## Done when\n{_list(t.get('acceptance'))}",
    ]
    if p:
        parts.append(f"## The plan it belongs to: {p.get('title')}\nGoal: {p.get('goal')}\n"
                     f"Out of scope (do not do):\n{_list(p.get('out_of_scope'))}")
    conventions = await store.conventions_for(conn, project, task["layers"])
    if conventions:
        parts.append("## Conventions for these layers\n" + "\n\n".join(
            f"### {c['body'].get('title')}\n{c['body'].get('rule', '')}" for c in conventions))
    feature_docs = await store.list_documents(conn, project, type="feature_doc", feature=task["feature"], limit=1) \
        if task["feature"] else []
    if feature_docs:
        parts.append(f"## How this feature works today\n{feature_docs[0]['body'].get('overview', '')}")
    if task["feature"]:
        handoffs = await store.list_documents(conn, project, type="handoff", feature=task["feature"], limit=3)
        friction = [f for h in handoffs for f in h["body"].get("friction", [])]
        if friction:
            parts.append("## Friction seen before in this feature (avoid repeating it)\n" + _list(friction[:8]))
    gate_doc = await store.latest_of_type(conn, project, "gate_config")
    if gate_doc:
        cfg = GateConfig.from_yaml(gate_doc["body"]["yaml"])
        fast = [g.name for g in cfg.gates if g.fast]
        parts.append(
            "## Gates\nWhen you finish, the metronome runs every gate: "
            + ", ".join(g.name for g in cfg.gates)
            + f". While working, run the fast ones ({', '.join(fast) or 'none'}) with `autonome-gates --fast`."
            " Never weaken, skip or edit a gate or its rules to make it pass."
        )
    return "\n\n".join(parts)[:_MAX_CHARS]


async def _interactive(conn: AsyncConnection, project: str) -> str:
    conventions = await store.conventions_for(conn, project)
    drafts = await store.list_documents(conn, project, type="plan", status="draft", limit=5)
    lines = [
        f"autonome: this project ({project}) has a memory in autonome-context (MCP server `autonome`).",
        "Search it (tool `search`) before planning or changing a feature: earlier decisions, deviations and"
        " friction live there. Get the rules for the layers you touch with `conventions_get`.",
    ]
    if conventions:
        lines.append("Conventions: " + "; ".join(
            f"{c['body'].get('title')} ({', '.join(c['layers']) or 'all layers'})" for c in conventions))
    if drafts:
        lines.append("Draft plans waiting for approval: " + "; ".join(
            f"#{d['id']} {d['body'].get('title')}" for d in drafts))
    return "\n".join(lines)
