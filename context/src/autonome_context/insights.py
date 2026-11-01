"""What the history says about the process itself: the tune loop's input.

Facts come from data (gate runs, task outcomes, plan adherence in handoffs). Friction is free text,
so it is grouped by meaning: each item is embedded and similar items are clustered.
"""

import math
from collections import Counter, defaultdict
from datetime import UTC, datetime

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from . import briefing, store
from .embed import Embedder
from .history import triage_entry

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


def _when(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _convention_effects(conventions: list[dict], tasks: list[dict], friction: list[dict],
                              embedder: Embedder) -> list[dict]:
    """For each active convention: did what it targets drop after it was adopted?"""
    if not conventions:
        return []
    rules = [f"{c['body'].get('title', '')}\n{c['body'].get('rule', '')}".strip() for c in conventions]
    rule_vecs = await embedder.embed(rules, "document") if friction else [[] for _ in conventions]
    friction_vecs = await embedder.embed([i["text"] for i in friction], "document") if friction else []
    effects = []
    for c, vec in zip(conventions, rule_vecs):
        since = _when(c["created_at"])
        title = (c["body"].get("title") or "").lower()
        if since is None or not title:
            continue
        before = after = tasks_after = 0
        for t in tasks:
            runs = t["body"].get("runs") or []
            if any((at := _when(r.get("at"))) and at > since for r in runs):
                tasks_after += 1
            for r in runs:
                at = _when(r.get("at"))
                if at is None:
                    continue
                for round_ in r.get("reviews") or [r.get("review") or {}]:
                    for f in round_.get("findings", []):
                        if (f.get("convention") or "").lower() == title or title in f.get("issue", "").lower():
                            if at > since:
                                after += 1
                            else:
                                before += 1
        f_before = f_after = 0
        for item, ivec in zip(friction, friction_vecs):
            at = _when(item["created_at"])
            if at is None or _cosine(vec, ivec) < _SIMILAR:
                continue
            if at > since:
                f_after += 1
            else:
                f_before += 1
        recurrences = after + f_after
        holding = True if tasks_after >= 3 and recurrences == 0 else False if recurrences >= 2 else None
        effects.append({"convention_id": c["id"], "title": c["body"].get("title"), "since": since.isoformat(),
                        "findings_before": before, "findings_after": after,
                        "friction_before": f_before, "friction_after": f_after,
                        "tasks_after": tasks_after, "holding": holding})
    effects.sort(key=lambda e: -(e["findings_after"] + e["friction_after"]))
    return effects[:30]


async def _triage(conn: AsyncConnection, project: str, tasks: list[dict]) -> dict:
    """How the triage judged deviations and what happened next."""
    decisions = {d["id"]: d for d in await store.list_documents(conn, project, type="decision", limit=None)
                 if d["body"].get("deviation")}
    judged = [e for d in decisions.values() if (e := triage_entry(d))]
    exhausted = [{"decision_id": d["id"], "title": d["body"].get("title"), "feature": d["feature"]}
                 for d in decisions.values() if d["body"].get("triage_exhausted")]
    undone = []
    for t in tasks:
        source = str(t["body"].get("source", ""))
        if t["status"] != "done" or not source.startswith("decision:") or not source[9:].isdigit():
            continue
        d = decisions.get(int(source[9:]))
        if not d or d["status"] != "rejected":
            continue
        runs = sorted(await store.gate_runs(conn, task_id=t["id"]), key=lambda r: r["id"])
        # The task's last run: the newest session's gates, the latest result of each.
        last = {r["gate"]: r["passed"] for r in runs if r["session_id"] == runs[-1]["session_id"]}
        undone.append({"decision_id": d["id"], "title": d["body"].get("title"), "feature": d["feature"],
                       "task_id": t["id"], "judged_by": (triage_entry(d) or {}).get("backend", "human"),
                       "gates_passed": all(last.values()) if last else None})
    rejected = Counter(d["feature"] for d in decisions.values() if d["status"] == "rejected" and d["feature"])
    return {"by_verdict": dict(Counter(j["verdict"] for j in judged)),
            "by_backend": dict(Counter(j["backend"] for j in judged)),
            "exhausted": exhausted, "undone": undone,
            "recurring": [{"feature": f, "rejected": n} for f, n in rejected.most_common() if n >= 2]}


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
        friction += [{"text": f, "feature": h["feature"], "handoff_id": h["id"],
                      "created_at": h["created_at"]} for f in b.get("friction", [])]

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
            """select g.task_id, count(*) filter (where not g.passed) as failures
               from gate_run g join project p on p.id = g.project_id and p.slug = %s
               where g.task_id is not null group by g.task_id""", (project,))
        gate_failures = {r["task_id"]: r["failures"] for r in await cur.fetchall()}

    tasks = await store.list_documents(conn, project, type="task", limit=500)
    # An encore is any trip back to the implementer: a failed gate, or a review round after the first.
    runs_of = lambda t: t["body"].get("runs") or []
    review_encores = {t["id"]: sum(max((r.get("review_rounds") or 1) - 1, 0) for r in runs_of(t)) for t in tasks}
    played = [t["id"] for t in tasks if runs_of(t)]
    encores = [gate_failures.get(i, 0) + review_encores.get(i, 0) for i in played]
    outcomes = Counter(t["status"] for t in tasks)
    # What the reviewer noticed but did not block on: the raw material for new conventions.
    findings = [{"task_id": t["id"], "feature": t["feature"], "kind": f.get("kind"), "severity": f["severity"],
                 "convention": f.get("convention"), "issue": f["issue"][:400]}
                for t in tasks for r in (t["body"].get("runs") or [])[-1:]
                for f in ((r.get("reviews") or [r.get("review") or {}])[-1]).get("findings", [])
                if f.get("severity") != "high"]
    loose_ends = [{"task_id": t["id"], "title": t["body"]["title"], "feature": t["feature"],
                   "loose_ends": t["body"]["playback_review"]["loose_ends"]}
                  for t in tasks if (t["body"].get("playback_review") or {}).get("status") == "pending"
                  and t["body"]["playback_review"].get("loose_ends")]
    spent = sorted(({"task_id": t["id"], "title": t["body"]["title"],
                     "tokens": sum(r.get("tokens") or 0 for r in runs_of(t)),
                     "cost_usd": round(sum(r.get("cost_usd") or 0 for r in runs_of(t)), 2),
                     "review_rounds": sum(r.get("review_rounds") or 0 for r in runs_of(t))} for t in tasks if runs_of(t)),
                   key=lambda s: -s["cost_usd"])
    # A convention that eats a big share of the briefing crowds out the task, the plan and the rest.
    conventions = await store.list_documents(conn, project, type="convention", status="active", limit=200)
    heavy = [{"convention_id": c["id"], "title": c["body"].get("title"), "chars": len(c["body"].get("rule", "")),
              "briefing_share": round(len(c["body"].get("rule", "")) / briefing.MAX_CHARS, 2)}
             for c in conventions if len(c["body"].get("rule", "")) > briefing.MAX_CHARS // 4]
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
        "spent": {"tokens": sum(s["tokens"] for s in spent), "cost_usd": round(sum(s["cost_usd"] for s in spent), 2),
                  "costliest": spent[:5]},
        "loose_ends": loose_ends,
        "heavy_conventions": heavy,
        "convention_effects": await _convention_effects(conventions, tasks, friction, embedder),
        "task_outcomes": dict(outcomes),
        "blocked": blocked,
        "review_findings": findings[:60],
        "triage": await _triage(conn, project, tasks),
        "friction_clusters": await _friction_clusters(friction, embedder),
    }
