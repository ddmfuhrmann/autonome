"""MCP server for agents plus a small HTTP API for hooks, in one process."""

import asyncio
import base64
import gzip
from dataclasses import asdict
from typing import Annotated

import uvicorn
from mcp.server.mcpserver import MCPServer
from psycopg_pool import AsyncConnectionPool
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import config, store
from .chunking import DEFAULT_STRATEGY, STRATEGIES
from .embed import Embedder, OllamaEmbedder
from .handoff import Handoff
from .migrate import migrate

settings = config.load()
mcp = MCPServer(
    name="autonome-context",
    instructions=(
        "Memory of a software project, built from handoffs of past AI sessions. "
        "Search it before planning or changing a feature to find earlier decisions, deviations and friction."
    ),
)

_pool: AsyncConnectionPool | None = None
_embedder: Embedder | None = None


_init_lock = asyncio.Lock()


async def _services() -> tuple[AsyncConnectionPool, Embedder]:
    global _pool, _embedder
    async with _init_lock:
        if _pool is None:
            _pool = AsyncConnectionPool(settings.database_url, open=False)
            await _pool.open()
        if _embedder is None:
            _embedder = await OllamaEmbedder.create(settings.ollama_url, settings.embed_model)
    return _pool, _embedder


async def _put(h: Handoff, raw_transcript: str | None) -> int:
    pool, embedder = await _services()
    async with pool.connection() as conn:
        return await store.put_handoff(conn, h, raw_transcript, STRATEGIES[DEFAULT_STRATEGY], embedder)


@mcp.tool()
async def handoff_put(handoff: Handoff) -> dict:
    """Store the handoff of a finished unit of work and index it for search."""
    return {"document_id": await _put(handoff, None)}


@mcp.tool()
async def search(
    query: Annotated[str, Field(description="Natural language or exact terms, e.g. class names")],
    project: Annotated[str, Field(description="Project slug")],
    feature: str | None = None,
    layer: str | None = None,
    type: Annotated[str | None, Field(description="plan, task, handoff, decision, ...")] = None,
    k: int = 8,
) -> list[dict]:
    """Hybrid (vector + full-text) search over the project's memory."""
    pool, embedder = await _services()
    async with pool.connection() as conn:
        hits = await store.search(
            conn, query, project, STRATEGIES[DEFAULT_STRATEGY], embedder,
            feature=feature, layer=layer, doc_type=type, k=k,
        )
    return [asdict(h) for h in hits]


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
    return JSONResponse({"document_id": await _put(h, transcript)}, status_code=201)


def main() -> None:
    for name in migrate(settings.database_url):
        print(f"applied migration {name}")
    app = mcp.streamable_http_app(host=settings.host)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
