"""What the history says about the process itself: the tune loop's input.

Facts come from data (gate runs, task outcomes, plan adherence in handoffs). Friction is free text,
so it is grouped by meaning: each item is embedded and similar items are clustered.
"""

import math
from collections import Counter, defaultdict

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from . import store
from .embed import Embedder

_SIMILAR = 0.82  # cosine similarity above which two friction items count as the same problem


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1)


async def _friction_clusters(items: list[dict], embedder: Embedder) -> list[dict]:
    if not items:
        return []
    vectors = await embedder.embed([i["text"] for i in items], "document")
    clusters: list[dict] = []
    for item, vec in zip(items, vectors):
        best = max(clusters, key=lambda c: _cosine(c["centroid"], vec), default=None)
        if best and _cosine(best["centroid"], vec) >= _SIMILAR:
            best["items"].append(item)
            n = len(best["items"])
            best["centroid"] = [(c * (n - 1) + v) / n for c, v in zip(best["centroid"], vec)]
        else:
            clusters.append({"centroid": vec, "items": [item]})
    repeated = [c for c in clusters if len({i["handoff_id"] for i in c["items"]}) >= 2]
    repeated.sort(key=lambda c: -len(c["items"]))
    return [{"size": len(c["items"]), "example": c["items"][0]["text"],
             "features": sorted({i["feature"] for i in c["items"] if i["feature"]}),
             "handoffs": sorted({i["handoff_id"] for i in c["items"]})} for c in repeated]


async def build(conn: AsyncConnection, project: str, embedder: Embedder, limit: int = 200) -> dict:
    handoffs = await store.list_documents(conn, project, type="handoff", limit=limit)

    adherence: dict[tuple, list[float]] = defaultdict(list)
    plan_items: dict[str, Counter] = defaultdict(Counter)
    friction: list[dict] = []
    for h in handoffs:
        b = h["body"]
        for layer in h["layers"] or ["-"]:
            if b.get("adherence_score") is not None:
                adherence[(h["feature"] or "-", layer)].append(b["adherence_score"])
        for item in b.get("plan_adherence", []):
            plan_items[h["feature"] or "-"][item["status"]] += 1
        friction += [{"text": f, "feature": h["feature"], "handoff_id": h["id"]} for f in b.get("friction", [])]

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """select g.gate, count(*) as runs, count(*) filter (where not g.passed) as failures,
                      avg(g.duration_ms)::int as avg_ms
               from gate_run g join project p on p.id = g.project_id and p.slug = %s
               group by g.gate order by failures desc""", (project,))
        gates = await cur.fetchall()
        await cur.execute(
            """select g.gate, g.signature, count(*) as times, count(distinct g.session_id) as sessions,
                      (array_agg(right(g.output, 400) order by g.created_at desc))[1] as last_output
               from gate_run g join project p on p.id = g.project_id and p.slug = %s
               where not g.passed and g.signature is not null
               group by g.gate, g.signature having count(distinct g.session_id) >= 2
               order by count(*) desc limit 10""", (project,))
        repeated_failures = await cur.fetchall()
        await cur.execute(
            """select g.task_id, count(*) filter (where not g.passed) as encores
               from gate_run g join project p on p.id = g.project_id and p.slug = %s
               where g.task_id is not null group by g.task_id""", (project,))
        encores = [r["encores"] for r in await cur.fetchall()]

    tasks = await store.list_documents(conn, project, type="task", limit=500)
    outcomes = Counter(t["status"] for t in tasks)
    blocked = [{"task_id": t["id"], "title": t["body"]["title"], "feature": t["feature"],
                "reason": (t["body"].get("runs") or [{}])[-1].get("reason", "")[:300]}
               for t in tasks if t["status"] == "blocked"]

    return {
        "handoffs": len(handoffs),
        "adherence": sorted(
            ({"feature": f, "layer": l, "avg": sum(v) / len(v), "n": len(v)} for (f, l), v in adherence.items()),
            key=lambda r: r["avg"]),
        "plan_items": {f: dict(c) for f, c in plan_items.items()},
        "gates": gates,
        "repeated_failures": repeated_failures,
        "encores_per_task": {"tasks": len(encores), "avg": sum(encores) / len(encores) if encores else 0,
                             "max": max(encores, default=0)},
        "task_outcomes": dict(outcomes),
        "blocked": blocked,
        "friction_clusters": await _friction_clusters(friction, embedder),
    }
