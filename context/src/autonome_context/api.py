"""HTTP API for hooks and the runner (the plugin's scripts are stdlib-only, so no MCP client)."""

import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import briefing, services, store
from .gates import GateConfig


def jsonable(data):
    """Dates and other rich values as strings."""
    return json.loads(json.dumps(data, default=str))


def _json(data, status_code: int = 200) -> JSONResponse:
    return JSONResponse(jsonable(data), status_code=status_code)


async def gate_config_body(conn, project: str) -> dict:
    doc = await store.latest_of_type(conn, project, "gate_config")
    if not doc:
        return {"version": None, "config": GateConfig().model_dump(mode="json")}
    return {"version": doc["version"], "config": GateConfig.from_yaml(doc["body"]["yaml"]).model_dump(mode="json")}


async def get_gate_config(request: Request) -> JSONResponse:
    pool, _ = await services.get()
    async with pool.connection() as conn:
        return _json(await gate_config_body(conn, request.path_params["project"]))


async def post_gate_runs(request: Request) -> JSONResponse:
    body = await request.json()
    pool, _ = await services.get()
    async with pool.connection() as conn:
        await store.record_gate_runs(
            conn, body["project"], body["session_id"], body["runs"],
            task_id=body.get("task_id"), config_version=body.get("config_version"),
        )
    return _json({"recorded": len(body["runs"])}, 201)


async def get_gate_runs(request: Request) -> JSONResponse:
    p = request.query_params
    pool, _ = await services.get()
    async with pool.connection() as conn:
        runs = await store.gate_runs(
            conn, project=p.get("project"), session_id=p.get("session_id"),
            task_id=int(p["task_id"]) if p.get("task_id") else None,
        )
    return _json(runs)


async def get_briefing(request: Request) -> JSONResponse:
    task_id = request.query_params.get("task_id")
    pool, _ = await services.get()
    async with pool.connection() as conn:
        text = await briefing.build(conn, request.path_params["project"], int(task_id) if task_id else None)
    return _json({"text": text})


def routes() -> list:
    return [
        Route("/api/projects/{project}/briefing", get_briefing),
        Route("/api/projects/{project}/gate-config", get_gate_config),
        Route("/api/gate-runs", post_gate_runs, methods=["POST"]),
        Route("/api/gate-runs", get_gate_runs),
    ]
