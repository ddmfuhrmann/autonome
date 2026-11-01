"""Server-rendered UI: the human side of the context (setlist, approvals, memory, setup)."""

import json
import logging
import re
from dataclasses import asdict
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

import markdown as md
from markupsafe import Markup

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import evaluation, insights, plans, rules, services, store
from .chunking import DEFAULT_STRATEGY, STRATEGIES
from .gates import EXAMPLE, GateConfig

log = logging.getLogger(__name__)
_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=_HERE / "templates")
templates.env.filters["pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False, default=str)
_LIST_ITEM = re.compile(r"^( *)([-*+]|\d+[.)]) ")


def _loose_lists(text: str) -> str:
    """Agents write lists right under a paragraph and nest them by 2-3 spaces; Python-Markdown needs a
    blank line before a list and 4 spaces per level, or the list melts into the paragraph."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        m = None if fenced else _LIST_ITEM.match(line)
        if m:
            depth = len(m[1])
            if 0 < depth < 4 or depth % 4:
                line = "    " * max(1, round(depth / 3)) + line.lstrip()
            prev = out[-1] if out else ""
            if prev.strip() and not _LIST_ITEM.match(prev) and not prev.startswith(" "):
                out.append("")
        out.append(line)
    return "\n".join(out)


# Agent-written markdown: escape raw HTML, then render.
templates.env.filters["md"] = lambda v: Markup(md.markdown(
    _loose_lists(str(v or "").replace("<", "&lt;")), extensions=["tables", "fenced_code", "sane_lists"]))



def _md_inline(v) -> Markup:
    """One line of agent-written markdown (a list item, a criterion): `code`, **bold**, links."""
    html = md.markdown(str(v or "").replace("<", "&lt;"))
    return Markup(html[3:-4] if html.startswith("<p>") and html.endswith("</p>") and html.count("<p>") == 1 else html)


templates.env.filters["mdi"] = _md_inline

DOC_TYPES = ["handoff", "plan", "task", "decision", "convention", "gate_config", "design", "feature_doc", "wave", "backlog"]
MODES = ["vector", "hybrid", "fts"]
REF_PREFIX = {"task": "T", "plan": "P", "wave": "W", "decision": "D", "handoff": "H", "convention": "C"}
STATUS_LABELS = {"on_hold": "On hold", "merge-failed": "Merge failed", "fts": "Full-text"}
SEVERITY = {"high": "blocker", "medium": "major", "low": "minor"}


def ref(v, type: str | None = None) -> str:
    """T-21, P-15, W-18: the short id a human reads; other documents keep #id."""
    doc_id, type = (v["id"], v["type"]) if isinstance(v, dict) else (v, type)
    prefix = REF_PREFIX.get(type or "")
    return f"{prefix}-{doc_id}" if prefix else f"#{doc_id}"


def label(status) -> str:
    s = str(status or "")
    return STATUS_LABELS.get(s) or s.replace("_", " ").capitalize()


def _when(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def ago(v) -> str:
    t = _when(v)
    if t is None:
        return ""
    s = int((datetime.now(UTC) - t).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    if s < 7 * 86400:
        return f"{s // 86400}d ago"
    return t.strftime("%b %d")


def when(v) -> str:
    t = _when(v)
    return t.strftime("%b %d, %H:%M") if t else ""


def tokens(n) -> str:
    n = n or 0
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def money(x) -> str:
    return f"${x or 0:.2f}"


def parse_diff(text: str | None) -> list[dict]:
    """A unified diff, split into files with numbered rows for the .diff table."""
    files: list[dict] = []
    f = None
    old = new = 0
    for line in (text or "").splitlines():
        if line.startswith("diff --git"):
            f = {"path": line.split(" b/", 1)[-1], "add": 0, "del": 0, "rows": []}
            files.append(f)
        elif f is None or line.startswith(("index ", "--- ", "+++ ", "new file", "deleted file", "similarity",
                                            "rename ", "old mode", "new mode", "Binary files")):
            if f is not None and line.startswith("Binary files"):
                f["rows"].append(("hunk", "", "binary file"))
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            old, new = (int(m[1]), int(m[2])) if m else (0, 0)
            f["rows"].append(("hunk", "", line))
        elif line.startswith("+"):
            f["rows"].append(("add", new, line[1:]))
            f["add"] += 1
            new += 1
        elif line.startswith("-"):
            f["rows"].append(("del", old, line[1:]))
            f["del"] += 1
            old += 1
        elif not line.startswith("\\"):
            f["rows"].append(("ctx", new, line[1:]))
            old += 1
            new += 1
    return files


for _name, _fn in {"ref": ref, "label": label, "ago": ago, "when": when, "tokens": tokens, "money": money,
                   "diff_files": parse_diff, "spent": lambda t: _cost(t), "sev": lambda s: SEVERITY.get(s, "minor")}.items():
    templates.env.filters[_name] = _fn


# --- what is waiting on the human ----------------------------------------------------------------

QUEUE_KINDS = {
    "plans": "Waves & plans", "held": "Held by rehearsal", "soundcheck": "Soundcheck", "fermata": "Fermata",
    "blocked": "Blocked tasks", "playback": "Playback", "decision": "Decisions", "convention": "Conventions",
}


# What holds up the setlist. Decisions and conventions are reviewed when the human likes.
BLOCKING = {"plans", "held", "soundcheck", "fermata", "blocked"}


async def _queue(conn, project: str) -> list[dict]:
    """Everything only a human can move, most blocking first."""
    docs = lambda type, status: store.list_documents(conn, project, type=type, status=status)
    items = [{"kind": "fermata", "doc": d} for d in await docs("task", "on_hold")]
    items += [{"kind": "held", "doc": d} for d in await docs("plan", "held")]
    items += [{"kind": "soundcheck", "doc": d} for d in await docs("plan", "blocked")]
    items += [{"kind": "blocked", "doc": d} for d in await docs("task", "blocked")]
    items += [{"kind": "plans", "doc": d} for d in await docs("wave", "draft")]
    items += [{"kind": "plans", "doc": d} for d in await docs("plan", "draft") if not await plans.wave_of(conn, d)]
    items += [{"kind": "playback", "doc": t} for t in await docs("task", "done")
              if (t["body"].get("playback_review") or {}).get("status") == "pending"]
    triage = await _triage_on(conn, project)  # with triage on, the classifier judges deviations only
    items += [{"kind": "decision", "doc": d} for d in await docs("decision", "proposed")
              if not (triage and d["body"].get("deviation"))]
    items += [{"kind": "convention", "doc": d} for d in await docs("convention", "proposed")]
    return items


async def _triage_on(conn, project: str) -> bool:
    cfg = await _gate_config(conn, project)
    return bool(cfg and cfg.triage and cfg.triage.model != "off")


async def _side(conn, project: str) -> dict:
    """The sidebar's live part: what waits on the human and what the maestro is playing."""
    tasks = await store.list_documents(conn, project, type="task", limit=500)
    status = Counter(t["status"] for t in tasks)
    waves = await store.list_documents(conn, project, type="wave", status="approved", limit=5)
    queue = await _queue(conn, project)
    waiting = [i for i in queue if i["kind"] in BLOCKING]
    return {"waiting": len(waiting), "to_hear": sum(i["kind"] == "playback" for i in queue), "playing": status["playing"],
            "on_hold": status["on_hold"], "pending": status["pending"], "wave": waves[0] if waves else None}


async def render(request: Request, name: str, **ctx) -> Response:
    project = request.path_params.get("project")
    side = None
    if project:
        pool, _ = await services.get()
        async with pool.connection() as conn:
            side = await _side(conn, project)
    return templates.TemplateResponse(request, name, {"project": project, "side": side, **ctx})


# --- decisions: the buttons a human gets, and what each one does ----------------------------------

EFFECTS = {
    ("wave", "draft", "approved"): "Every plan in the wave is approved; their tasks enter the setlist and play "
                                   "in the order shown, each plan after the ones it depends on.",
    ("wave", "draft", "abandoned"): "The wave is dropped. Its plans stay drafts and can be approved on their own.",
    ("wave", "approved", "abandoned"): "The wave is dropped. Its plans keep their own status; merged work stays on main.",
    ("plan", "draft", "approved"): "Its tasks enter the setlist and the maestro plays them in order.",
    ("plan", "draft", "abandoned"): "The plan is dropped; nothing is queued.",
    ("plan", "approved", "abandoned"): "No task of the plan is played any more. Merged work stays on main.",
    ("plan", "held", "revised"): "The unplayed tasks are replaced by the revised ones and the plan starts.",
    ("plan", "held", "approved"): "The plan starts as written; the rehearsal notes stay attached to it.",
    ("plan", "held", "abandoned"): "The plan is dropped; plans that wait for it never start.",
    ("plan", "blocked", "approved"): "One more soundcheck, with fresh fix rounds.",
    ("plan", "blocked", "done"): "The feature is done as it is; the plans after it may play.",
    ("plan", "blocked", "abandoned"): "The plan is dropped; plans that wait for it never start.",
    ("task", "on_hold", "released"): "The maestro merges the branch into main next.",
    ("task", "on_hold", "pending"): "Back to pending: a new session plays it again.",
    ("task", "blocked", "pending"): "Back to pending: a new session plays it again.",
    ("task", "blocked", "abandoned"): "The task is dropped; tasks that depend on it never start.",
    ("task", "pending", "abandoned"): "The task is dropped; tasks that depend on it never start.",
    ("convention", "proposed", "active"): "Injected into every agent session that touches its layers.",
    ("convention", "proposed", "rejected"): "Dropped; tune will not propose it again from the same evidence.",
    ("decision", "proposed", "accepted"): "Becomes a decision in effect for its feature; later plans respect it.",
    ("decision", "proposed", "rejected"): "A task to undo it is queued in the maintenance plan.",
    ("decision", "accepted", "superseded"): "Marked as no longer in effect.",
}
ACTION_LABELS = {
    ("plan", "held", "revised"): "Accept revision", ("plan", "held", "approved"): "Keep original",
    ("plan", "blocked", "approved"): "Try again", ("plan", "blocked", "done"): "Accept as is",
    ("task", "on_hold", "released"): "Release to merge", ("task", "on_hold", "pending"): "Play again",
    ("task", "blocked", "pending"): "Try again", ("convention", "proposed", "active"): "Adopt",
}
QUESTIONS = {
    ("wave", "draft"): "Approve the whole wave?", ("plan", "draft"): "Approve this plan?",
    ("plan", "held"): "Accept the revised plan?", ("plan", "blocked"): "The soundcheck kept failing. What now?",
    ("task", "on_hold"): "Release this task to merge?", ("task", "blocked"): "Play it again?",
    ("decision", "proposed"): "Accept this decision?", ("convention", "proposed"): "Adopt this convention?",
}
VERBS = {"approved": "Approve", "accepted": "Accept", "rejected": "Reject", "abandoned": "Abandon",
         "released": "Release", "active": "Adopt", "superseded": "Supersede", "pending": "Play again", "done": "Done"}
DANGER = {"abandoned", "rejected", "superseded"}
APPROVE = {"approved", "accepted", "released", "revised", "active"}


def actions(doc: dict, in_wave: dict | None = None) -> list[dict]:
    type_, status = doc["type"], doc["status"] or ""
    if type_ == "plan" and status == "held":
        tos = ["revised", "approved", "abandoned"] if doc["body"].get("rehearsal", {}).get("plan") \
            else ["approved", "abandoned"]
    elif in_wave and status == "draft":
        tos = []  # approved with its wave
    else:
        tos = [to for (t, frm, to), actors in rules.TRANSITIONS.items()
               if t == type_ and frm == status and "human" in actors]
    out = []
    for to in tos:
        key = (type_, status, to)
        out.append({"to": to, "label": ACTION_LABELS.get(key) or VERBS.get(to) or label(to),
                    "effect": EFFECTS.get(key, "") + (f" It archives #{doc['body']['replaces']}, which it replaces."
                                                      if key == ("convention", "proposed", "active") and doc["body"].get("replaces") else ""),
                    "style": "danger" if to in DANGER else "approve" if to in APPROVE else ""})
    # One recommended choice: the first approving one, else the first non-destructive one.
    for a in [a for a in out if a["style"] == "approve"][1:]:
        a["style"] = ""
    if out and not any(a["style"] == "approve" for a in out):
        for a in out:
            if a["style"] == "":
                a["style"] = "primary"
                break
    return out


async def _retire_replaced(conn, convention: dict) -> None:
    """Adopting a convention that replaces another archives the old one: two active rules that
    contradict each other leave every agent guessing which one holds."""
    old_id = convention["body"].get("replaces")
    old = await store.get_document(conn, int(old_id)) if old_id else None
    if old and old["type"] == "convention" and old["project"] == convention["project"] and old["status"] == "active":
        await store.set_status(conn, old["id"], "archived")


async def decide(conn, indexer, doc: dict, to: str) -> str:
    """Applies a human's decision, from the UI or confirmed in a session (approval_resolve). Returns
    the new status."""
    doc_id = doc["id"]
    if doc["type"] == "plan" and doc["status"] == "held" and to in ("revised", "approved"):
        await plans.resolve_rehearsal(conn, indexer, doc_id, accept=to == "revised")
    elif doc["type"] == "plan" and doc["status"] == "blocked" and to == "approved":
        await plans.retry_soundcheck(conn, indexer, doc_id)
    elif doc["type"] == "plan" and doc["status"] == "blocked" and to == "done":
        await plans.accept_as_is(conn, doc_id)
    elif doc["type"] == "plan" and to == "approved":
        await plans.approve(conn, indexer, doc_id)
    elif doc["type"] == "wave" and to == "approved":
        await plans.approve_wave(conn, indexer, doc_id)
    elif doc["type"] == "decision" and doc["status"] == "proposed" and to == "rejected":
        await plans.reject_decision(conn, indexer, doc_id)
    elif doc["type"] == "plan" and to == "abandoned":
        async with conn.transaction():
            await rules.transition(conn, doc_id, "abandoned", "human")
            if doc["status"] != "draft":
                await plans.mark_backlog(conn, doc, "next")
    elif doc["type"] == "convention" and doc["status"] == "proposed" and to == "active":
        async with conn.transaction():
            await rules.transition(conn, doc_id, "active", "human")
            await _retire_replaced(conn, doc)
    else:
        await rules.transition(conn, doc_id, to, "human")
    return (await store.get_document(conn, doc_id))["status"]


def _asset_version() -> str:
    """Changes whenever a static file does, so browsers fetch the new CSS after a deploy."""
    import hashlib
    h = hashlib.sha1()
    for f in sorted((_HERE / "static").rglob("*")):
        if f.is_file():
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


templates.env.globals["asset_v"] = _asset_version()
templates.env.globals["question"] = lambda doc: QUESTIONS.get((doc["type"], doc["status"] or ""))


# --- reading tasks and plans ---------------------------------------------------------------------


def _cost(t: dict) -> tuple[int, float]:
    runs = t["body"].get("runs") or []
    return sum(r.get("tokens") or 0 for r in runs), sum(r.get("cost_usd") or 0 for r in runs)


def duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


templates.env.filters["duration"] = duration


def _progress(tasks: list[dict]) -> dict:
    """Counts and bar widths (in %) of a group of tasks."""
    live = [t for t in tasks if t["status"] != "abandoned"]
    c = Counter(t["status"] for t in live)
    n = len(live) or 1
    tok, usd = (sum(x) for x in zip(*[_cost(t) for t in tasks])) if tasks else (0, 0.0)
    return {"total": len(live), "counts": c, "done": c["done"], "tokens": tok, "cost": usd,
            "bar": {"done": 100 * c["done"] / n, "playing": 100 * (c["playing"] + c["released"]) / n,
                    "held": 100 * c["on_hold"] / n, "blocked": 100 * c["blocked"] / n}}


async def _tasks_of(conn, plan_id: int) -> list[dict]:
    ids = [l["id"] for l in await store.links_of(conn, plan_id) if l["kind"] == "has_task" and l["direction"] == "out"]
    tasks = [await store.get_document(conn, i) for i in ids]
    return sorted(tasks, key=lambda t: t["body"].get("seq", 0))


async def _gate_config(conn, project: str) -> GateConfig | None:
    doc = await store.latest_of_type(conn, project, "gate_config")
    try:
        return GateConfig.from_yaml(doc["body"]["yaml"]) if doc else None
    except Exception:
        return None


def _mermaid(tasks: list[dict]) -> str:
    lines = ["graph LR"]
    for t in tasks:
        lines.append(f'  T{t["id"]}["{ref(t)}"]')
        lines += [f'  T{d} --> T{t["id"]}' for d in t["body"].get("depends_on", [])]
    return "\n".join(lines)


def _stages(run: dict | None, status: str) -> list[tuple[str, str]]:
    """session → gates → review rN → merge, as far as the last run got."""
    if not run:
        return []
    review = run.get("review")
    names = ["session", "gates"] + ([f"review r{run.get('review_rounds') or 1}"] if review else []) + ["merge"]
    outcome = run.get("outcome")
    if outcome == "merged":
        return [(n, "is-done") for n in names]
    if outcome == "on_hold" or status in ("on_hold", "released"):
        names[-1] = "fermata" if status == "on_hold" else "merge"
        return [(n, "is-done") for n in names[:-1]] + [(names[-1], "is-current")]
    if review and review.get("verdict") == "fail":
        return [(n, "is-done") for n in names[:2]] + [(names[2], "is-failed"), (names[3], "")]
    return []


# --- pages ---------------------------------------------------------------------------------------


async def home(request: Request) -> Response:
    pool, _ = await services.get()
    async with pool.connection() as conn:
        projects = await store.list_projects(conn)
    return await render(request, "home.html", projects=projects)


async def _readable(conn, hits: list) -> list[dict]:
    """Hits with their document's title and status, and the chunk text without its context prefix."""
    out = []
    for hit in hits:
        h = asdict(hit)
        d = await store.get_document(conn, h["document_id"])
        h["title"] = (d["body"].get("title") or d["body"].get("summary") or "") if d else ""
        h["status"] = d["status"] if d else None
        _, sep, rest = h["text"].partition("\nsection: ")
        h["snippet"] = rest.partition("\n")[2] if sep else h["text"]
        out.append(h)
    return out


async def search_page(request: Request) -> Response:
    p = request.query_params
    q = p.get("q", "").strip()
    mode = p.get("mode") if p.get("mode") in MODES else "vector"
    strategy = p.get("strategy") if p.get("strategy") in STRATEGIES else DEFAULT_STRATEGY
    hits = []
    if q:
        pool, indexer = await services.get()
        async with pool.connection() as conn:
            hits = await store.search(
                conn, q, request.path_params["project"], STRATEGIES[strategy], indexer.embedder,
                feature=p.get("feature") or None, layer=p.get("layer") or None,
                doc_type=p.get("type") or None, mode=mode, k=20,
            )
    if hits:
        pool, _ = await services.get()
        async with pool.connection() as conn:
            hits = await _readable(conn, hits)
    return await render(request, "search.html", q=q, mode=mode, strategy=strategy, hits=hits,
                        strategies=list(STRATEGIES), modes=MODES, types=DOC_TYPES, filters=p)


async def lab_page(request: Request) -> Response:
    project = request.path_params["project"]
    q = request.query_params.get("q", "").strip()
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        counts = {
            r[0]: r[1] for r in await (await conn.execute(
                "select s.name, count(c.id) filter (where p.slug = %s) from strategy s"
                " left join chunk c on c.strategy_id = s.id"
                " left join document_version dv on dv.id = c.document_version_id"
                " left join document d on d.id = dv.document_id"
                " left join project p on p.id = d.project_id group by s.name", (project,))).fetchall()
        }
        compare = {}
        if q:
            for name, strategy in STRATEGIES.items():
                compare[name] = await _readable(conn, await store.search(conn, q, project, strategy, indexer.embedder, k=5))
        questions = await evaluation.questions(conn, project)
        runs = await evaluation.runs(conn, project)
    strategies = [{"name": n, "params": s.params, "chunks": counts.get(n, 0)} for n, s in STRATEGIES.items()]
    best = {m: max((r[m] for r in runs), default=None) for m in ("recall", "mrr", "ndcg")}
    return await render(request, "lab.html", q=q, strategies=strategies, compare=compare,
                        questions=questions, runs=runs, best=best)


async def lab_eval(request: Request) -> Response:
    project = request.path_params["project"]
    k = int((await request.form()).get("k", 5))
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        for strategy in STRATEGIES.values():
            for mode in MODES:
                await evaluation.run(conn, project, strategy, indexer.embedder, mode, k)
    return RedirectResponse(f"/ui/p/{project}/lab", status_code=303)


async def lab_question(request: Request) -> Response:
    project = request.path_params["project"]
    form = await request.form()
    expected = [int(x) for x in str(form.get("expected", "")).replace(",", " ").split() if x.isdigit()]
    if form.get("question") and expected:
        pool, _ = await services.get()
        async with pool.connection() as conn:
            await evaluation.add_question(conn, project, str(form["question"]), expected, "manual")
    return RedirectResponse(f"/ui/p/{project}/lab", status_code=303)


DOC_FACETS = ("type", "status", "feature", "layer")
PEEK_HIDDEN = {"title", "project", "feature", "layers", "session_id", "plan_id", "seq", "wave_id", "kind",
               "yaml", "diff", "diff_stat", "output", "mermaid", "last_output", "transcript"}


def query_url(request: Request, *, toggle: tuple[str, str] | None = None, put: dict | None = None,
              drop: tuple[str, ...] = ()) -> str:
    """This page's URL with one query change: toggle a facet value, put (replace) keys, or drop keys.
    Changing a filter closes the drawer, since the open document may no longer be in the list."""
    items = [(k, v) for k, v in request.query_params.multi_items() if k not in drop]
    if toggle:
        items = [(k, v) for k, v in items if k != "doc"]
        items.remove(toggle) if toggle in items else items.append(toggle)
    for k, v in (put or {}).items():
        items = [(a, b) for a, b in items if a != k]
        if v not in (None, ""):
            items.append((k, str(v)))
    q = urlencode(items)
    return request.url.path + (f"?{q}" if q else "")


def day_groups(docs: list[dict], now: datetime | None = None) -> list[tuple[str, list[dict]]]:
    """Today, Yesterday, then one group per date; docs keep their order (newest first)."""
    today = (now or datetime.now(UTC)).date()
    groups: list[tuple[str, list[dict]]] = []
    for d in docs:
        day = _when(d["created_at"]).date()
        delta = (today - day).days
        name = "Today" if delta == 0 else "Yesterday" if delta == 1 else day.strftime("%a, %b %d")
        if not groups or groups[-1][0] != name:
            groups.append((name, []))
        groups[-1][1].append(d)
    return groups


def excerpt(body: dict, limit: int = 3) -> list[tuple[str, str]]:
    """The first few prose fields of a body, summary first, for the drawer."""
    keys = (["summary"] if body.get("summary") else []) + [k for k in body if k != "summary"]
    out = []
    for k in keys:
        v = body.get(k)
        if k in PEEK_HIDDEN or not isinstance(v, str) or not v.strip():
            continue
        out.append((k.replace("_", " ").capitalize(), v if len(v) <= 600 else v[:600].rsplit(" ", 1)[0] + "…"))
        if len(out) == limit:
            break
    return out


async def documents_page(request: Request) -> Response:
    """Facets on the left, the list grouped by day, and the picked document in a drawer (?doc=ID)."""
    project = request.path_params["project"]
    p = request.query_params
    f = store.DocFilter(types=tuple(p.getlist("type")), statuses=tuple(p.getlist("status")),
                        features=tuple(p.getlist("feature")), layers=tuple(p.getlist("layer")),
                        q=p.get("q", ""))
    pool, _ = await services.get()
    async with pool.connection() as conn:
        docs = await store.browse_documents(conn, project, f)
        facets = await store.document_facets(conn, project, f)
        peek = None
        if (pick := p.get("doc", "")).isdigit():
            doc = await store.get_document(conn, int(pick))
            if doc and doc["project"] == project:
                links = await store.links_of(conn, doc["id"])
                names = await store.titles(conn, [l["id"] for l in links])
                ids = [d["id"] for d in docs]
                i = ids.index(doc["id"]) if doc["id"] in ids else None
                peek = {"doc": doc, "links": [{**l, "title": names.get(l["id"], "")} for l in links],
                        "excerpt": excerpt(doc["body"]),
                        "prev": ids[i - 1] if i else None,
                        "next": ids[i + 1] if i is not None and i + 1 < len(ids) else None}
    selected = {k: p.getlist(k) for k in DOC_FACETS}
    return await render(request, "documents.html", groups=day_groups(docs), total=len(docs), facets=facets,
                        selected=selected, q=f.q, peek=peek, url=lambda **kw: query_url(request, **kw))


async def document_page(request: Request) -> Response:
    project = request.path_params["project"]
    pool, _ = await services.get()
    async with pool.connection() as conn:
        doc = await store.get_document(conn, int(request.path_params["doc_id"]))
        if not doc or doc["project"] != project:
            return Response("not found", status_code=404)
        links = await store.links_of(conn, doc["id"])
        in_wave = await plans.wave_of(conn, doc) if doc["type"] == "plan" else None
        ctx = {"doc": doc, "links": links, "in_wave": in_wave, "actions": actions(doc, in_wave),
               "nav_section": NAV_SECTION.get(doc["type"], "documents")}
        if doc["type"] == "wave":
            return await render(request, "wave.html", **ctx, **await _wave(conn, doc))
        if doc["type"] == "plan":
            return await render(request, "plan.html", **ctx, **await _plan(conn, doc))
        if doc["type"] == "task":
            return await render(request, "task.html", **ctx, **await _task(conn, doc))
    return await render(request, "document.html", **ctx)


NAV_SECTION = {"wave": "waves", "plan": "waves", "task": "setlist", "convention": "conventions",
               "gate_config": "config"}


async def _wave(conn, wave: dict) -> dict:
    feats = []
    for i, f in enumerate(wave["body"]["features"], start=1):
        plan = await store.get_document(conn, f["plan_id"])
        tasks = await _tasks_of(conn, f["plan_id"]) if plan else []
        feats.append({"pos": i, "plan": plan, "tasks": tasks, "progress": _progress(tasks),
                      "after": [wave["body"]["features"][d - 1]["plan_id"] for d in f.get("depends_on", [])
                                if 0 < d <= len(wave["body"]["features"])]})
    all_tasks = [t for f in feats for t in f["tasks"]]
    failures = Counter(g["task_id"] for g in await store.gate_runs(conn, project=wave["project"], limit=5000)
                       if not g["passed"])
    return {"features": feats, "progress": _progress(all_tasks), "summary": _run_summary(feats, failures)}


def _run_summary(feats: list[dict], gate_failures: Counter) -> dict:
    """How the wave played, task by task: what the maestro prints when it stops, kept on the page."""
    rows = []
    for f in feats:
        for t in f["tasks"]:
            runs = t["body"].get("runs") or []
            last = runs[-1] if runs else {}
            tokens, cost = _cost(t)
            rows.append({
                "task": t, "feature": f["plan"]["feature"] if f["plan"] else None, "sessions": len(runs),
                "encores": gate_failures.get(t["id"], 0),
                "review_rounds": max((r.get("review_rounds") or 0 for r in runs), default=0),
                "duration_s": sum(r.get("duration_s") or 0 for r in runs), "tokens": tokens, "cost": cost,
                "reason": last.get("reason") if t["status"] in ("blocked", "on_hold") else None,
                "playback": t["body"].get("playback_review"),
            })
    totals = {k: sum(r[k] for r in rows) for k in ("sessions", "encores", "review_rounds", "duration_s", "tokens", "cost")}
    counts = Counter(r["task"]["status"] for r in rows)
    return {"rows": rows, "totals": totals, "merged": counts["done"], "blocked": counts["blocked"],
            "held": counts["on_hold"]}


async def _plan(conn, plan: dict) -> dict:
    tasks = await _tasks_of(conn, plan["id"])
    failures = Counter(g["task_id"] for g in await store.gate_runs(conn, project=plan["project"], limit=5000)
                       if not g["passed"])
    cfg = await _gate_config(conn, plan["project"])
    return {"tasks": tasks, "progress": _progress(tasks), "encores": failures, "cfg": cfg,
            "graph": _mermaid(tasks) if tasks else None,
            "after": sorted({p for t in tasks for p in t["body"].get("after_plans", [])})}


async def _task(conn, task: dict) -> dict:
    runs = task["body"].get("runs") or []
    gate_runs = await store.gate_runs(conn, task_id=task["id"], limit=500)
    by_session: dict[str, list] = {}
    for g in reversed(gate_runs):
        by_session.setdefault(g["session_id"], []).append(g)
    plan = await store.get_document(conn, task["body"]["plan_id"]) if task["body"].get("plan_id") else None
    last = runs[-1] if runs else None
    diff_run = next((r for r in reversed(runs) if r.get("diff")), None)
    summary = {}
    for sid, gs in by_session.items():
        per: dict[str, dict] = {}
        for g in gs:  # oldest first: the last one wins
            s = per.setdefault(g["gate"], {"gate": g["gate"], "runs": 0, "failures": 0, "ms": 0})
            s.update(runs=s["runs"] + 1, failures=s["failures"] + (not g["passed"]), passed=g["passed"],
                     ms=g["duration_ms"], at=g["created_at"], output=g["output"] if not g["passed"] else s.get("output"))
        summary[sid] = list(per.values())
    return {"runs": runs, "gates_by_session": summary, "plan": plan, "last": last, "diff_run": diff_run,
            "stages": _stages(last, task["status"]), "cfg": await _gate_config(conn, task["project"]),
            "encores": sum(1 for g in gate_runs if not g["passed"]),
            "events": await store.events(conn, task_id=task["id"], limit=400)}


async def config_page(request: Request) -> Response:
    project = request.path_params["project"]
    error, text, summary = None, None, None
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        if request.method == "POST":
            text = str((await request.form()).get("yaml", ""))
            try:
                GateConfig.from_yaml(text)
            except Exception as e:  # YAML or schema error: show it, keep the text
                error = str(e)
            else:
                await store.save_singleton(conn, indexer, project, "gate_config", {"yaml": text})
                return RedirectResponse(f"/ui/p/{project}/config", status_code=303)
        doc = await store.latest_of_type(conn, project, "gate_config")
        runs = await store.gate_runs(conn, project=project, limit=30)
    if text is None:
        text = doc["body"]["yaml"] if doc else EXAMPLE
        try:
            cfg = GateConfig.from_yaml(text)
            summary = f"{len(cfg.gates)} gates" + (", soundcheck configured" if cfg.soundcheck else "")
        except Exception as e:
            error = str(e)
    return await render(request, "config.html", text=text, doc=doc, error=error, runs=runs, summary=summary)


async def approvals_page(request: Request) -> Response:
    project = request.path_params["project"]
    kind = request.query_params.get("kind")
    pool, _ = await services.get()
    async with pool.connection() as conn:
        everything = await _queue(conn, project)
        items = [i for i in everything if i["kind"] in BLOCKING]
        review = [i for i in everything if i["kind"] not in BLOCKING]
        counts = Counter(i["kind"] for i in everything)
        shown = [i for i in everything if i["kind"] == kind] if kind else items
        for item in shown + (review if not kind else []):
            doc = item["doc"]
            item["actions"] = actions(doc)
            if doc["type"] == "wave":
                item["plans"] = [await store.get_document(conn, f["plan_id"]) for f in doc["body"]["features"]]
            elif doc["type"] == "plan":
                item["tasks_count"] = len(await _tasks_of(conn, doc["id"])) or len(doc["body"].get("tasks", []))
            elif doc["type"] == "task":
                item["plan"] = await store.get_document(conn, doc["body"]["plan_id"])
                item["gate_failures"] = sum(not g["passed"] for g in await store.gate_runs(conn, task_id=doc["id"], limit=200))
                item["last"] = (doc["body"].get("runs") or [None])[-1]
                if doc["status"] == "blocked":
                    item["failed_gate"] = next((g for g in await store.gate_runs(conn, task_id=doc["id"], limit=50)
                                                if not g["passed"]), None)
            elif doc["type"] == "plan" and doc["status"] == "held":
                item["tasks"] = await _tasks_of(conn, doc["id"])
    oldest = min(items, key=lambda i: i["doc"]["created_at"]) if items else None
    return await render(request, "approvals.html", items=shown, review=review, total=len(items), counts=counts,
                        kind=kind, kinds=QUEUE_KINDS, oldest=oldest)


async def playback(request: Request) -> Response:
    """The human's review after merge: heard, or a fix queued as a new task. Never blocks anything."""
    project, doc_id = request.path_params["project"], int(request.path_params["doc_id"])
    form = await request.form()
    verdict, reason = str(form.get("verdict") or ""), str(form.get("reason") or "")
    back = str(form.get("next") or "")
    if not back.startswith(f"/ui/p/{project}/"):
        back = f"/ui/p/{project}/documents/{doc_id}"
    from_script = request.headers.get("x-requested-with") == "fetch"

    def answer(payload: dict, status: int = 200) -> Response:
        if from_script:
            return JSONResponse(payload, status_code=status)
        if status != 200:
            return Response(f"{payload['error']}\n\nNothing changed. Go back: {back}", status_code=status)
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}{urlencode({'done': payload['notice']})}", status_code=303)

    try:
        pool, indexer = await services.get()
        async with pool.connection() as conn:
            doc = await store.get_document(conn, doc_id)
            if not doc or doc["project"] != project:
                return answer({"error": "not found"}, 404)
            if verdict == "heard":
                await plans.playback_heard(conn, indexer, doc_id)
                notice = f"#{doc_id}: sounds right"
            elif verdict == "fix":
                fix = await plans.playback_fix(conn, indexer, doc_id, reason)
                notice = f"#{doc_id}: fix queued as #{fix['task_id']}" + (
                    " (approve the maintenance plan once for it to play)" if fix.get("plan_status") == "draft" else "")
            else:
                return answer({"error": "the review did not reach the context (no verdict was sent)"}, 400)
    except rules.Forbidden as e:
        return answer({"error": str(e)}, 409)
    except Exception as e:
        log.exception("playback of #%s failed", doc_id)
        return answer({"error": f"the context could not save it ({type(e).__name__}); try again in a moment"}, 503)
    return answer({"status": verdict, "notice": notice, "location": back})


async def transition(request: Request) -> Response:
    """The human gate. Only reachable from the UI. The page script posts with X-Requested-With: fetch
    and gets JSON, so it can show the outcome and only then reflect the new status; a plain form post
    is redirected with a notice."""
    project, doc_id = request.path_params["project"], int(request.path_params["doc_id"])
    form = await request.form()
    to = str(form.get("to") or "")
    back = str(form.get("next") or "")
    if not back.startswith(f"/ui/p/{project}/"):
        back = f"/ui/p/{project}/documents/{doc_id}"
    from_script = request.headers.get("x-requested-with") == "fetch"

    def failed(message: str, status: int) -> Response:
        if from_script:
            return JSONResponse({"error": message}, status_code=status)
        return Response(f"{message}\n\nNothing changed. Go back: {back}", status_code=status)

    if not to:
        return failed("the decision did not reach the context (no target status was sent)", 400)
    try:
        pool, indexer = await services.get()
        async with pool.connection() as conn:
            doc = await store.get_document(conn, doc_id)
            if not doc or doc["project"] != project:
                return failed("not found", 404)
            now = await decide(conn, indexer, doc, to)
    except rules.Forbidden as e:
        return failed(str(e), 409)
    except Exception as e:  # the database or the context restarting: say so, never pretend it worked
        log.exception("transition of #%s to %s failed", doc_id, to)
        return failed(f"the context could not save it ({type(e).__name__}); try again in a moment", 503)
    notice = f"#{doc_id} is now {now}"
    if from_script:
        return JSONResponse({"status": now, "notice": notice, "location": back})
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}{urlencode({'done': notice})}", status_code=303)


async def conventions_page(request: Request) -> Response:
    project = request.path_params["project"]
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        if request.method == "POST":
            form = await request.form()
            title, rule = str(form.get("title", "")).strip(), str(form.get("rule", "")).strip()
            layers = [l.strip() for l in str(form.get("layers", "")).split(",") if l.strip()]
            if title and rule:
                async with conn.transaction():
                    await store.create_document(conn, indexer, project=project, type="convention", status="active",
                                                body={"title": title, "rule": rule}, layers=layers)
            return RedirectResponse(f"/ui/p/{project}/conventions", status_code=303)
        conventions = await store.list_documents(conn, project, type="convention")
    layer = request.query_params.get("layer")
    layers = Counter(l or "all" for c in conventions if c["status"] != "proposed" for l in (c["layers"] or [None]))
    shown = [c for c in conventions if c["status"] != "proposed"
             and (not layer or layer in (c["layers"] or ["all"]))]
    proposed = [c for c in conventions if c["status"] == "proposed"]
    return await render(request, "conventions.html", conventions=shown, proposed=proposed, layers=layers,
                        layer=layer, total=sum(1 for c in conventions if c["status"] != "proposed"))


async def convention_edit(request: Request) -> Response:
    project, doc_id = request.path_params["project"], int(request.path_params["doc_id"])
    form = await request.form()
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        doc = await store.get_document(conn, doc_id)
        if not doc or doc["project"] != project or doc["type"] != "convention":
            return Response("not found", status_code=404)
        if form.get("archive"):
            await store.set_status(conn, doc_id, "archived" if doc["status"] == "active" else "active")
        else:
            async with conn.transaction():
                # Only title and rule are edited here; why, replaces and evidence stay.
                await store.add_version(conn, indexer, doc_id, {**doc["body"], "title": str(form["title"]).strip(),
                                                                "rule": str(form["rule"]).strip()})
    return RedirectResponse(f"/ui/p/{project}/conventions", status_code=303)


SETLIST_COLUMNS = ["pending", "playing", "on_hold", "released", "blocked", "done"]
COLUMN_LIMIT = {"pending": 12, "done": 12}


async def setlist_page(request: Request) -> Response:
    project = request.path_params["project"]
    pool, _ = await services.get()
    async with pool.connection() as conn:
        tasks = await store.list_documents(conn, project, type="task", limit=500)
        plans_ = {d["id"]: d for d in await store.list_documents(conn, project, type="plan", limit=200)}
        waves = await store.list_documents(conn, project, type="wave", status="approved", limit=5)
    tasks.sort(key=lambda t: (t["body"].get("plan_id", 0), t["body"].get("seq", 0)))
    columns = {c: [t for t in tasks if t["status"] == c] for c in SETLIST_COLUMNS}
    columns["done"].reverse()
    totals = {c: len(ts) for c, ts in columns.items()}
    for c, n in COLUMN_LIMIT.items():
        columns[c] = columns[c][:n]
    status_of = {t["id"]: t["status"] for t in tasks} | {p["id"]: p["status"] for p in plans_.values()}
    async with pool.connection() as conn:
        now_doing = await store.last_events(conn, [t["id"] for t in tasks if t["status"] == "playing"])
    return await render(request, "setlist.html", columns=columns, totals=totals, plans=plans_, status_of=status_of,
                        wave=waves[0] if waves else None, progress=_progress(tasks),
                        playing=[t for t in tasks if t["status"] == "playing"], now_doing=now_doing)


async def waves_page(request: Request) -> Response:
    project = request.path_params["project"]
    pool, _ = await services.get()
    async with pool.connection() as conn:
        waves = []
        for w in await store.list_documents(conn, project, type="wave"):
            tasks = [t for f in w["body"]["features"] for t in await _tasks_of(conn, f["plan_id"])]
            waves.append({"doc": w, "progress": _progress(tasks)})
        backlog = sorted(await store.list_documents(conn, project, type="backlog", limit=100),
                         key=lambda b: b["body"].get("wave", 0))
    return await render(request, "waves.html", waves=waves, backlog=backlog)


async def features_page(request: Request) -> Response:
    project = request.path_params["project"]
    pool, _ = await services.get()
    async with pool.connection() as conn:
        feats = await store.features(conn, project)
        overview = await store.feature_doc(conn, project, None)
        latest_plan = {}
        for p in await store.list_documents(conn, project, type="plan", limit=500):
            latest_plan.setdefault(p["feature"], p)  # newest first
        docs = {f["slug"]: await store.feature_doc(conn, project, f["slug"]) for f in feats if f["documented"]}
    tooling = [slug for slug, d in docs.items() if d and d["body"].get("kind") == "tooling"]
    return await render(request, "features.html", features=feats, overview=overview, latest_plan=latest_plan,
                        docs=docs, tooling_slugs=tooling)


async def feature_page(request: Request) -> Response:
    project, feature = request.path_params["project"], request.path_params["feature"]
    pool, _ = await services.get()
    async with pool.connection() as conn:
        doc = await store.feature_doc(conn, project, feature)
        timeline = [d for d in await store.list_documents(conn, project, feature=feature, limit=300)
                    if d["type"] in ("plan", "handoff", "decision", "task")]
    return await render(request, "feature.html", feature=feature, doc=doc, timeline=timeline)


async def triage_page(request: Request) -> Response:
    """Deviations the runner's classifier judged, newest first, with the answers behind each verdict."""
    project = request.path_params["project"]
    pool, _ = await services.get()
    async with pool.connection() as conn:
        undo = {}
        for t in await store.list_documents(conn, project, type="task", limit=None):
            src = str(t["body"].get("source", ""))
            if src.startswith("decision:"):
                undo.setdefault(src.split(":", 1)[1], t)
        rows = []
        waiting = []  # proposed deviations the classifier has not judged (no key, backend down, runner crash)
        for d in await store.list_documents(conn, project, type="decision", limit=None):
            if not d["body"].get("triage_history"):
                if d["status"] == "proposed" and d["body"].get("deviation"):
                    waiting.append(d)
            else:
                rows.append({"doc": d, "triage": d["body"]["triage_history"][-1],
                             "exhausted": bool(d["body"].get("triage_exhausted")), "undo": undo.get(str(d["id"]))})
        on = await _triage_on(conn, project)
    rows.sort(key=lambda r: r["triage"].get("at") or "", reverse=True)
    return await render(request, "triage.html", rows=rows, on=on, waiting=waiting if on else [])


async def insights_page(request: Request) -> Response:
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        data = await insights.build(conn, request.path_params["project"], indexer.embedder)
    scored = [a for a in data["adherence"]]
    avg = sum(a["avg"] * a["n"] for a in scored) / sum(a["n"] for a in scored) if scored else None
    layers = sorted({a["layer"] for a in scored})
    grid: dict[str, dict] = {}
    for a in scored:
        grid.setdefault(a["feature"], {})[a["layer"]] = a["avg"]
    return await render(request, "insights.html", i=data, avg_adherence=avg, layers=layers, grid=grid,
                        gate_failures=sum(g["failures"] for g in data["gates"]))


def routes() -> list:
    return [
        Route("/", lambda r: RedirectResponse("/ui")),
        Route("/ui", home),
        Route("/ui/p/{project}", lambda r: RedirectResponse(f"/ui/p/{r.path_params['project']}/setlist")),
        Route("/ui/p/{project}/search", search_page),
        Route("/ui/p/{project}/lab", lab_page),
        Route("/ui/p/{project}/lab/eval", lab_eval, methods=["POST"]),
        Route("/ui/p/{project}/lab/questions", lab_question, methods=["POST"]),
        Route("/ui/p/{project}/documents", documents_page),
        Route("/ui/p/{project}/documents/{doc_id:int}", document_page),
        Route("/ui/p/{project}/config", config_page, methods=["GET", "POST"]),
        Route("/ui/p/{project}/approvals", approvals_page),
        Route("/ui/p/{project}/setlist", setlist_page),
        Route("/ui/p/{project}/waves", waves_page),
        Route("/ui/p/{project}/features", features_page),
        Route("/ui/p/{project}/insights", insights_page),
        Route("/ui/p/{project}/triage", triage_page),
        Route("/ui/p/{project}/features/{feature}", feature_page),
        Route("/ui/p/{project}/documents/{doc_id:int}/transition", transition, methods=["POST"]),
        Route("/ui/p/{project}/documents/{doc_id:int}/playback", playback, methods=["POST"]),
        Route("/ui/p/{project}/conventions", conventions_page, methods=["GET", "POST"]),
        Route("/ui/p/{project}/conventions/{doc_id:int}", convention_edit, methods=["POST"]),
        Mount("/static", StaticFiles(directory=_HERE / "static"), name="static"),
    ]
