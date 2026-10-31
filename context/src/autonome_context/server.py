"""One process: the MCP server for agents, an HTTP API for hooks and runners, and the web UI."""

import argparse
import asyncio
import logging
import base64
import gzip
from dataclasses import asdict
from typing import Annotated, Literal

import uvicorn
from mcp.server.mcpserver import MCPServer
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import api, evaluation, services, store, web
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
    return JSONResponse({"document_id": await _put_handoff(h, transcript)}, status_code=201)


def build_app():
    app = mcp.streamable_http_app(host=services.settings.host)
    app.router.routes.extend(api.routes() + web.routes())
    return app


async def _cli(args: argparse.Namespace) -> None:
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        if args.command == "reindex":
            names = [args.strategy] if args.strategy else list(STRATEGIES)
            for name in names:
                print(f"{name}: {await store.reindex(conn, STRATEGIES[name], indexer.embedder)} versions indexed")
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
