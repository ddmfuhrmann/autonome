"""What went off plan before, on the same subject: the lessons compose and the preflight read before
planning. Everything here is already in the memory, spread over handoffs, decisions, plans and tasks;
this gathers it for the features a subject touches."""

from psycopg import AsyncConnection

from . import store
from .chunking import DEFAULT_STRATEGY, STRATEGIES
from .embed import Embedder

_TEXT = 300  # characters kept per item: enough to read, small enough for a briefing
_PER_KIND = 12


def triage_entry(d: dict) -> dict | None:
    """The verdict that settled or still stands on a decision; None when the human settled it
    (also after the rounds ran out and it stayed proposed) or nothing judged it yet."""
    last = (d["body"].get("triage_history") or [None])[-1]
    if not last or d["status"] == "proposed":
        return last
    settled = {"accept": "accepted", "reject": "rejected"}.get(last["verdict"])  # an escalation is settled by the human
    return last if settled == d["status"] and not d["body"].get("triage_exhausted") else None


def _cut(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _TEXT else text[: _TEXT - 1] + "…"


async def related_features(conn: AsyncConnection, project: str, subject: str, embedder: Embedder,
                           feature: str | None = None) -> list[str]:
    """The named feature, then the features of the documents closest to the subject."""
    hits = await store.search(conn, subject, project, STRATEGIES[DEFAULT_STRATEGY], embedder, k=12)
    found = [feature] if feature else []
    for h in hits:
        if h.feature and h.feature not in found:
            found.append(h.feature)
    return found[:5]


async def build(conn: AsyncConnection, project: str, subject: str, embedder: Embedder,
                feature: str | None = None) -> dict:
    features = await related_features(conn, project, subject, embedder, feature)
    out: dict = {"subject": subject, "features": features, "plan_changes": [], "deviations": [],
                 "deviation_decisions": [], "preflight_revisions": [], "review_blocks": [], "fixes": [],
                 "blocked": [], "smoke_test_failures": [], "friction": []}
    for f in features:
        for h in await store.list_documents(conn, project, type="handoff", feature=f, limit=30):
            b = h["body"]
            out["plan_changes"] += [{"feature": f, "handoff": h["id"], "item": _cut(p["item"]), "status": p["status"],
                                     "note": _cut(p.get("note"))}
                                    for p in b.get("plan_adherence", []) if p.get("status") in ("changed", "skipped", "partial")]
            out["deviations"] += [{"feature": f, "handoff": h["id"], "text": _cut(d)} for d in b.get("deviations", [])]
            out["friction"] += [{"feature": f, "handoff": h["id"], "text": _cut(x)} for x in b.get("friction", [])]
        for d in await store.list_documents(conn, project, type="decision", feature=f, limit=100):
            if d["body"].get("deviation"):
                item = {"feature": f, "decision": d["id"], "status": d["status"],
                        "title": _cut(d["body"].get("title")), "why": _cut(d["body"].get("decision"))}
                judged = None if d["body"].get("undo_of_human_reject") else triage_entry(d)
                if judged:
                    item.update(judged_by=judged["backend"], verdict=judged["verdict"])
                elif d["status"] != "proposed":
                    item["judged_by"] = "human"
                out["deviation_decisions"].append(item)
        for p in await store.list_documents(conn, project, type="plan", feature=f, limit=30):
            r = p["body"].get("preflight") or {}
            if r.get("verdict") in ("revise", "revised", "kept"):
                out["preflight_revisions"].append({"feature": f, "plan": p["id"], "verdict": r["verdict"],
                                                   "notes": _cut(r.get("notes"))})
            for s in p["body"].get("smoke_tests", []):
                if not s.get("retry") and not s.get("passed"):
                    out["smoke_test_failures"].append({"feature": f, "plan": p["id"], "report": _cut(s.get("report"))})
        for t in await store.list_documents(conn, project, type="task", feature=f, limit=200):
            b = t["body"]
            runs = b.get("runs") or []
            for r in runs:
                seen = set()
                for rnd in r.get("reviews") or [r.get("review") or {}]:
                    for x in rnd.get("findings", []):
                        # A finding reported again in a later round is still one block.
                        if x.get("severity") == "high" and (x.get("kind"), x.get("issue")) not in seen:
                            seen.add((x.get("kind"), x.get("issue")))
                            out["review_blocks"].append({"feature": f, "task": t["id"], "kind": x.get("kind"),
                                                         "convention": x.get("convention"),
                                                         "issue": _cut(x.get("issue"))})
            if t["status"] == "blocked" and runs:
                out["blocked"].append({"feature": f, "task": t["id"], "title": _cut(b["title"]),
                                       "reason": _cut(runs[-1].get("reason"))})
            source = b.get("source") or ""
            if source.startswith(("smoke_test", "after_merge:", "decision:")):
                out["fixes"].append({"feature": f, "task": t["id"], "source": source.split(":")[0],
                                     "title": _cut(b["title"]), "why": _cut(b.get("spec"))})
    for kind, items in out.items():
        if isinstance(items, list) and kind != "features":
            out[kind] = items[:_PER_KIND]
    out["empty"] = not any(out[k] for k in out if k not in ("subject", "features", "empty"))
    return out
