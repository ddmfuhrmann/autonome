"""One process: the MCP server for agents, an HTTP API for hooks and runners, and the web UI."""

import argparse
import asyncio
import logging
import base64
import gzip
from dataclasses import asdict
from typing import Annotated, Literal

from pathlib import Path

import uvicorn
from mcp.server.elicitation import AcceptedElicitation
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import api, backlog, evaluation, history, imports, insights, plans, rules, services, store, web
from .chunking import DEFAULT_STRATEGY, STRATEGIES
from .handoff import Handoff
from .migrate import migrate

mcp = MCPServer(
    name="autonome-context",
    instructions=(
        "Memory of a software project, built from handoffs of past AI sessions, plans and decisions. "
        "Search it before planning or changing a feature to find earlier decisions, deviations and friction."
    ),
)


async def _put_handoff(h: Handoff, raw_transcript: str | None) -> int:
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        return await store.put_handoff(conn, indexer, h, raw_transcript)


@mcp.tool()
async def handoff_put(handoff: Handoff) -> dict:
    """Store the handoff of a finished unit of work and index it for search."""
    return {"document_id": await _put_handoff(handoff, None)}


@mcp.tool()
async def search(
    query: Annotated[str, Field(description="Natural language or exact terms, e.g. class names")],
    project: Annotated[str, Field(description="Project slug")],
    feature: str | None = None,
    layer: str | None = None,
    type: Annotated[str | None, Field(description="plan, task, handoff, decision, convention, ...")] = None,
    mode: Annotated[Literal["hybrid", "vector", "fts"], Field(
        description="vector (default) finds paraphrases and Portuguese questions over English text; fts for exact names")] = "vector",
    k: int = 8,
) -> list[dict]:
    """Search the project's memory. Returns the best chunk of each matching document."""
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        hits = await store.search(
            conn, query, project, STRATEGIES[DEFAULT_STRATEGY], indexer.embedder,
            feature=feature, layer=layer, doc_type=type, mode=mode, k=k,
        )
    return [asdict(h) for h in hits]


@mcp.tool()
async def gate_config_get(project: Annotated[str, Field(description="Project slug")]) -> dict:
    """The project's current gate configuration: which gates run, in which order, encore and budget."""
    pool, _ = await services.get()
    async with pool.connection() as conn:
        return await api.gate_config_body(conn, project)


@mcp.tool()
async def plan_draft(
    project: str,
    plan: plans.PlanBody,
    feature: Annotated[str | None, Field(description="kebab-case feature slug")] = None,
    layers: list[str] = [],
    plan_id: Annotated[int | None, Field(description="revise this draft instead of creating a new one")] = None,
) -> dict:
    """Save a draft plan for the human to approve in the UI. Agents cannot approve plans. Put the gates
    the plan needs and the gate config lacks in `plan.gates`: approving the plan adds them."""
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        try:
            doc_id = await plans.draft(conn, indexer, project, feature, layers, plan, plan_id)
        except rules.Forbidden as e:
            return {"error": str(e)}
    return {"plan_id": doc_id, "status": "draft",
            "approve_at": f"{services.settings.public_url}/ui/p/{project}/documents/{doc_id}"}


@mcp.tool()
async def wave_draft(
    project: str,
    wave: plans.WaveBody,
    wave_id: Annotated[int | None, Field(description="revise this draft instead of creating a new one")] = None,
) -> dict:
    """Group draft plans of several features into a wave the human approves at once in the UI. Each
    feature's plan is drafted first with plan_draft. Approved waves play in order; a feature that
    depends on others is checked against the code (rehearsed) before it plays."""
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        try:
            doc_id = await plans.draft_wave(conn, indexer, project, wave, wave_id)
        except rules.Forbidden as e:
            return {"error": str(e)}
    return {"wave_id": doc_id, "status": "draft",
            "approve_at": f"{services.settings.public_url}/ui/p/{project}/documents/{doc_id}"}


class _Approval(BaseModel):
    approve: bool = Field(default=True, title="Approve", description="Leave checked and accept to approve")


async def _ask_human(ctx: Context, question: str, link: str) -> str:
    """Asks the person in the session, through MCP elicitation: the client shows it to them and the
    agent never sees or answers it. Returns approved, declined, or unavailable."""
    try:
        result = await ctx.elicit(f"{question}\n\nOr decide in the UI: {link}", _Approval)
    except Exception:  # the client cannot ask (a headless conduct session, an old client)
        return "unavailable"
    return "approved" if isinstance(result, AcceptedElicitation) and result.data.approve else "declined"


async def _approve(ctx: Context, project: str, doc_id: int, kind: str) -> dict:
    link = f"{services.settings.public_url}/ui/p/{project}/documents/{doc_id}"
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        doc = await store.get_document(conn, doc_id)
        if not doc or doc["type"] != kind or doc["project"] != project:
            return {"error": f"{kind} {doc_id} does not exist in {project}"}
        if doc["status"] != "draft":
            return {"error": f"{kind} {doc_id} is {doc['status']}, not a draft"}
        if kind == "plan" and (wave := await plans.wave_of(conn, doc)):
            return {"error": f"plan {doc_id} belongs to wave #{wave['id']}; ask with wave_approve"}
        if kind == "plan":
            summary = plans.plan_summary(doc)
        else:
            summary = plans.wave_summary(doc, [await store.get_document(conn, f["plan_id"]) for f in doc["body"]["features"]])
    answer = await _ask_human(ctx, f"Approve this {kind}? Its tasks start playing when the maestro runs.\n\n{summary}", link)
    if answer != "approved":
        return {"status": "draft", "human": answer, "approve_at": link,
                "note": "Nothing was approved. The draft waits; the human can approve it in the UI."}
    async with pool.connection() as conn:
        try:
            if kind == "plan":
                await plans.approve(conn, indexer, doc_id)
            else:
                await plans.approve_wave(conn, indexer, doc_id)
        except rules.Forbidden as e:
            return {"error": str(e)}
    return {"status": "approved", "human": "approved", "url": link}


@mcp.tool()
async def plan_approve(project: str, plan_id: int, ctx: Context) -> dict:
    """Ask the human, right in their session, to approve a draft plan. The question and a summary of
    the stored plan go to the person through their client (MCP elicitation); the agent cannot answer
    it. If they decline, or the client cannot ask, the plan stays a draft for the UI."""
    return await _approve(ctx, project, plan_id, "plan")


@mcp.tool()
async def wave_approve(project: str, wave_id: int, ctx: Context) -> dict:
    """Ask the human, right in their session, to approve a draft wave and every plan in it, the same
    way as plan_approve."""
    return await _approve(ctx, project, wave_id, "wave")


def _cut(text, n: int = 1500) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[:n] + "…"


async def _detail(conn, kind: str, doc: dict) -> str:
    """What the human needs to decide an item, read from the stored document."""
    b = doc["body"]
    last = (b.get("runs") or [{}])[-1]
    if kind in ("blocked", "fermata"):
        return "\n".join(filter(None, [f"Plan #{b.get('plan_id')}" if b.get("plan_id") else "",
                                         f"Spec: {_cut(b.get('spec'), 600)}", f"Branch: {last.get('branch', '')}",
                                         f"Why it stopped: {_cut(last.get('reason'))}" if last.get("reason") else ""]))
    if kind == "held":
        r = b.get("rehearsal") or {}
        revised = r.get("plan") or {}
        return (f"Rehearsal notes: {_cut(r.get('notes'))}\n"
                + ("Revision proposed, tasks: " + "; ".join(t.get("title", "") for t in revised.get("tasks", []))
                   if revised else "No revision proposed."))
    if kind == "soundcheck":
        runs = [r for r in b.get("soundchecks", []) if r.get("report")]
        return f"Last soundcheck: {_cut(runs[-1]['report'])}" if runs else "No soundcheck report."
    if kind == "plans":
        if doc["type"] == "wave":
            return plans.wave_summary(doc, [await store.get_document(conn, f["plan_id"]) for f in b["features"]])
        return plans.plan_summary(doc)
    if kind == "playback":
        ends = (b.get("playback_review") or {}).get("loose_ends") or []
        return (f"Merged as {str(last.get('merged_sha', ''))[:10]}. Spec: {_cut(b.get('spec'), 800)}"
                + ("\nLeft open at merge:\n" + "\n".join(f"- {_cut(e, 400)}" for e in ends) if ends else ""))
    if kind == "decision":
        return "\n".join(filter(None, [f"Decision: {_cut(b.get('decision'))}", f"Context: {_cut(b.get('context'), 600)}",
                                         f"Consequences: {_cut(b.get('consequences'), 600)}"]))
    return f"Rule: {_cut(b.get('rule'))}\nWhy: {_cut(b.get('why'), 600)}\nLayers: {', '.join(doc['layers'] or []) or 'all'}"


async def _choices(conn, kind: str, doc: dict) -> list[dict]:
    if kind == "playback":
        return [{"action": "heard", "label": "Sounds right", "effect": "The review is closed."},
                {"action": "fix", "label": "Needs a fix", "effect": "Your reason becomes a fix task in the maintenance plan."}]
    in_wave = await plans.wave_of(conn, doc) if doc["type"] == "plan" else None
    return [{"action": a["to"], "label": a["label"], "effect": a["effect"]} for a in web.actions(doc, in_wave)]


@mcp.tool()
async def approvals_list(project: str) -> list[dict]:
    """What waits on the human, most blocking first, with what each needs to be decided and the actions
    available. `blocks` is true for what holds up the setlist; the rest is reviewed when they like.
    Bring each item to the person, discuss it, and resolve it with approval_resolve."""
    pool, _ = await services.get()
    async with pool.connection() as conn:
        out = []
        for i in await web._queue(conn, project):
            d = i["doc"]
            out.append({"id": d["id"], "kind": i["kind"], "type": d["type"], "status": d["status"],
                        "title": d["body"].get("title") or _cut(d["body"].get("summary"), 80),
                        "feature": d["feature"], "blocks": i["kind"] in web.BLOCKING,
                        "detail": await _detail(conn, i["kind"], d), "actions": await _choices(conn, i["kind"], d),
                        "url": f"{services.settings.public_url}/ui/p/{project}/documents/{d['id']}"})
    return out


@mcp.tool()
async def approval_resolve(
    project: str,
    doc_id: int,
    action: Annotated[str, Field(description="one of the item's actions from approvals_list")],
    ctx: Context,
    reason: Annotated[str, Field(description="for a playback fix: what to fix; it becomes the fix task's spec")] = "",
    note: Annotated[str, Field(description="why you recommend it, shown to the human as your words")] = "",
) -> dict:
    """Resolve one waiting item, only with the human's yes: the item and the chosen action go to the
    person through their client (MCP elicitation), and the agent cannot answer it. Discuss it with
    them first; declined or unavailable leaves it waiting for the UI."""
    pool, indexer = await services.get()
    link = f"{services.settings.public_url}/ui/p/{project}/documents/{doc_id}"
    async with pool.connection() as conn:
        doc = await store.get_document(conn, doc_id)
        if not doc or doc["project"] != project:
            return {"error": f"#{doc_id} does not exist in {project}"}
        item = next((i for i in await web._queue(conn, project) if i["doc"]["id"] == doc_id), None)
        if not item:
            return {"error": f"#{doc_id} is {doc['status']} and waits on nobody"}
        choice = next((c for c in await _choices(conn, item["kind"], doc) if c["action"] == action), None)
        if not choice:
            return {"error": f"{action!r} is not an action for #{doc_id}; use one from approvals_list"}
        if action == "fix" and not reason.strip():
            return {"error": "a fix needs a reason: it becomes the fix task's instruction"}
        title = doc["body"].get("title") or doc["type"]
        question = (f"{web.QUESTIONS.get((doc['type'], doc['status']), 'Resolve this?')}\n\n"
                    f"#{doc_id} {title} ({doc['type']}, {doc['status']})\n\n"
                    f"Action: {choice['label']}. {choice['effect']}"
                    + (f"\n\nFix to queue: {reason}" if reason else "")
                    + (f"\n\nThe agent's reasoning: {note}" if note else ""))
    answer = await _ask_human(ctx, question, link)
    if answer != "approved":
        return {"status": doc["status"], "human": answer, "url": link,
                "note": "Nothing changed. The item waits; the human can decide it in the UI."}
    async with pool.connection() as conn:
        try:
            if action == "heard":
                await plans.playback_heard(conn, indexer, doc_id)
                return {"status": "heard", "human": "approved", "url": link}
            if action == "fix":
                fix = await plans.playback_fix(conn, indexer, doc_id, reason)
                return {"status": "fix queued", "fix_task_id": fix["task_id"], "human": "approved", "url": link}
            return {"status": await web.decide(conn, indexer, doc, action), "human": "approved", "url": link}
        except rules.Forbidden as e:
            return {"error": str(e)}


@mcp.tool()
async def history_get(
    project: str,
    subject: Annotated[str, Field(description="What is being planned, in plain words")],
    feature: Annotated[str | None, Field(description="The feature slug, if known")] = None,
) -> dict:
    """What went off plan before on the same subject (the feature and the closest related ones): plan
    items changed or skipped, deviations, decisions taken against the plan and how the human judged them,
    rehearsal revisions, reviewer blocks, fixes from soundcheck, playback and rejected decisions, blocked
    tasks and friction. Read it before planning, and turn each pattern into acceptance, out of scope, a
    convention or a question."""
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        return api.jsonable(await history.build(conn, project, subject, indexer.embedder, feature))


@mcp.tool()
async def backlog_list(project: str, wave: Annotated[int | None, Field(description="only this wave")] = None) -> list[dict]:
    """The backlog by wave (the roadmap's waves): each wave's title and status, and its items with id,
    title, status (backlog, next, doing, done), note and prd."""
    pool, _ = await services.get()
    async with pool.connection() as conn:
        return api.jsonable(await backlog.list_backlog(conn, project, wave))


@mcp.tool()
async def backlog_item_update(
    project: str,
    item_id: Annotated[str, Field(description="the item's id, e.g. w1-ci")],
    status: Annotated[str | None, Field(description="backlog, next, doing or done")] = None,
    title: str | None = None,
    note: str | None = None,
    prd: bool | str | None = None,
    wave: Annotated[int | None, Field(description="move the item to this wave")] = None,
) -> dict:
    """Change a backlog item, or move it to another wave. Use it to record what a planning conversation
    decided (an item moved, left out, its note). Every change is kept with who and when."""
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        try:
            return api.jsonable(await backlog.update_item(conn, indexer, project, item_id, by="agent:mcp", wave=wave,
                                                          status=status, title=title, note=note, prd=prd))
        except rules.Forbidden as e:
            return {"error": str(e)}


@mcp.tool()
async def backlog_item_add(project: str, wave: int, item_id: str, title: str, status: str = "backlog",
                           note: str | None = None, prd: bool | str | None = None) -> dict:
    """Add an item to a wave of the backlog. Ids are unique in the project (e.g. w2-audit)."""
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        try:
            return api.jsonable(await backlog.add_item(conn, indexer, project, wave, item_id, title, by="agent:mcp",
                                                       status=status, note=note, prd=prd))
        except rules.Forbidden as e:
            return {"error": str(e)}


@mcp.tool()
async def backlog_item_remove(project: str, item_id: str,
                              reason: Annotated[str, Field(description="why it leaves the backlog")] = "") -> dict:
    """Take an item out of the backlog. Its earlier versions and the reason are kept."""
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        try:
            return api.jsonable(await backlog.remove_item(conn, indexer, project, item_id, by="agent:mcp", reason=reason))
        except rules.Forbidden as e:
            return {"error": str(e)}


@mcp.tool()
async def plan_get(plan_id: int) -> dict:
    """A plan with its status and the status of its tasks."""
    pool, _ = await services.get()
    async with pool.connection() as conn:
        doc = await store.get_document(conn, plan_id)
        if not doc or doc["type"] != "plan":
            return {"error": f"plan {plan_id} not found"}
        tasks = [l for l in await store.links_of(conn, plan_id) if l["kind"] == "has_task"]
    return api.jsonable({**doc, "tasks": tasks})


@mcp.tool()
async def plan_list(project: str, status: str | None = None) -> list[dict]:
    """Plans of a project, newest first: id, title, status, feature."""
    pool, _ = await services.get()
    async with pool.connection() as conn:
        docs = await store.list_documents(conn, project, type="plan", status=status, limit=50)
    return [{"id": d["id"], "title": d["body"].get("title"), "status": d["status"], "feature": d["feature"]}
            for d in docs]


@mcp.tool()
async def conventions_get(
    project: str,
    layers: Annotated[list[str] | None, Field(description="only conventions for these layers; omit for all")] = None,
) -> list[dict]:
    """The project's conventions (code style, testing, patterns...). Read-only: humans edit them in the UI."""
    pool, _ = await services.get()
    async with pool.connection() as conn:
        docs = await store.conventions_for(conn, project, layers)
    return [{"id": d["id"], "title": d["body"].get("title"), "layers": d["layers"], "rule": d["body"].get("rule")}
            for d in docs]


@mcp.tool()
async def decision_propose(project: str, title: str, decision: str, context: str = "",
                           consequences: str = "", feature: str | None = None, layers: list[str] = []) -> dict:
    """Propose an architectural or product decision. A human accepts or rejects it in the UI."""
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        async with conn.transaction():
            doc_id = await store.create_document(
                conn, indexer, project=project, type="decision", status="proposed", feature=feature, layers=layers,
                body={"title": title, "context": context, "decision": decision, "consequences": consequences})
    return {"decision_id": doc_id, "status": "proposed"}


@mcp.tool()
async def insights_get(project: str) -> dict:
    """How the process is going: adherence by feature and layer, plan items skipped or changed, gate
    failure rates, failures that repeat across sessions, encores, blocked tasks, and friction grouped
    by meaning. The tune loop reads this."""
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        return api.jsonable(await insights.build(conn, project, indexer.embedder))


@mcp.tool()
async def convention_propose(project: str, title: str, rule: str, why: str, layers: list[str] = [],
                             replaces: Annotated[int | None, Field(description="id of the convention it would replace")] = None,
                             evidence: Annotated[list[int], Field(description="ids of the documents that justify it: tasks, handoffs, plans")] = []) -> dict:
    """Suggest a new or changed convention. It stays proposed until a human activates it in the UI."""
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        async with conn.transaction():
            doc_id = await store.create_document(
                conn, indexer, project=project, type="convention", status="proposed", layers=layers,
                body={"title": title, "rule": rule, "why": why, "replaces": replaces, "evidence": evidence})
            for e in evidence:
                found = await store.get_document(conn, e)
                if found and found["project"] == project:
                    await store.link(conn, doc_id, e, "evidence")
    return {"convention_id": doc_id, "status": "proposed"}


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/api/handoffs", methods=["POST"])
async def post_handoff(request: Request) -> JSONResponse:
    """Used by the SessionEnd hook: {"handoff": {...}, "raw_transcript_gz_b64": "..."}."""
    body = await request.json()
    try:
        h = Handoff.model_validate(body.get("handoff"))
    except ValidationError as e:
        return JSONResponse({"error": "invalid_handoff", "detail": e.errors()}, status_code=422)
    raw = body.get("raw_transcript_gz_b64")
    transcript = gzip.decompress(base64.b64decode(raw)).decode() if raw else None
    doc_id = await _put_handoff(h, transcript)
    if body.get("task_id"):
        pool, _ = await services.get()
        async with pool.connection() as conn:
            await store.link(conn, doc_id, int(body["task_id"]), "reports")
            await plans.note_handoff(conn, None, int(body["task_id"]), [i.model_dump() for i in h.plan_adherence])
    return JSONResponse({"document_id": doc_id}, status_code=201)


def build_app():
    # Only local clients, even when bound to 0.0.0.0 inside a container (DNS rebinding protection).
    local = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )
    app = mcp.streamable_http_app(transport_security=local)
    app.router.routes.extend(api.routes() + web.routes())
    return app


async def _cli(args: argparse.Namespace) -> None:
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        if args.command == "reindex":
            names = [args.strategy] if args.strategy else list(STRATEGIES)
            for name in names:
                print(f"{name}: {await store.reindex(conn, STRATEGIES[name], indexer.embedder)} versions indexed")
        elif args.command == "import-conventions":
            for path in sorted(Path(args.dir).glob("*.md")):
                text = path.read_text()
                title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("#")), path.stem)
                async with conn.transaction():
                    await store.create_document(conn, indexer, project=args.project, type="convention",
                                                status="active", body={"title": title, "rule": text},
                                                layers=args.layer or [])
                print(f"imported {path.name}")
        elif args.command == "import-adrs":
            for line in await imports.import_adrs(conn, indexer, args.project, Path(args.dir)):
                print(line)
        elif args.command == "import-backlog":
            waves = imports.parse_roadmap(Path(args.roadmap).read_text())
            for line in await imports.import_backlog(conn, indexer, args.project, waves):
                print(line)
        elif args.command == "delete-project":
            counts = await store.delete_project(conn, args.project)
            print(", ".join(f"{t} {n}" for t, n in counts.items() if n) or f"no project {args.project}")
        elif args.command == "eval-generate":
            print(f"{await evaluation.generate(conn, args.project, args.per_document)} questions created")
        elif args.command == "eval-run":
            for name in STRATEGIES:
                for mode in ("hybrid", "vector", "fts"):
                    print(await evaluation.run(conn, args.project, STRATEGIES[name], indexer.embedder, mode, args.k))


def main() -> None:
    parser = argparse.ArgumentParser(prog="autonome-context")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="migrate and serve MCP, API and UI (default)")
    reindex = sub.add_parser("reindex", help="index documents a strategy has not seen yet")
    reindex.add_argument("--strategy", choices=list(STRATEGIES))
    conv = sub.add_parser("import-conventions", help="import every *.md in a folder as a convention")
    conv.add_argument("--project", required=True)
    conv.add_argument("--dir", required=True)
    conv.add_argument("--layer", action="append", help="layers the conventions apply to (repeatable)")
    adrs = sub.add_parser("import-adrs", help="import every ADR (*.md) in a folder as a decision; again updates")
    adrs.add_argument("--project", required=True)
    adrs.add_argument("--dir", required=True)
    backlog = sub.add_parser("import-backlog", help="import a kanban roadmap's JSON block as one backlog per wave")
    backlog.add_argument("--project", required=True)
    backlog.add_argument("--roadmap", required=True, help="the roadmap HTML file")
    delete = sub.add_parser("delete-project", help="remove a project and everything in it (back up first)")
    delete.add_argument("--project", required=True)
    generate = sub.add_parser("eval-generate", help="synthetic eval questions from handoffs and decisions")
    generate.add_argument("--project", required=True)
    generate.add_argument("--per-document", type=int, default=2)
    run = sub.add_parser("eval-run", help="evaluate every strategy and search mode")
    run.add_argument("--project", required=True)
    run.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)

    for name in migrate(services.settings.database_url):
        print(f"applied migration {name}")
    if args.command in (None, "serve"):
        uvicorn.run(build_app(), host=services.settings.host, port=services.settings.port)
    else:
        asyncio.run(_cli(args))


if __name__ == "__main__":
    main()
