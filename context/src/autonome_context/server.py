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

from . import api, evaluation, insights, plans, rules, services, store, web
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
    mode: Literal["hybrid", "vector", "fts"] = "hybrid",
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
