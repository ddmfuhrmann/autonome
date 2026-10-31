"""HTTP API for hooks and the runner (the plugin's scripts are stdlib-only, so no MCP client)."""

import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import briefing, rules, services, store
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


async def claim_task(request: Request) -> JSONResponse:
    """The runner asks for work. A pending task is moved to playing; a released one is returned as is."""
    pool, _ = await services.get()
    async with pool.connection() as conn:
        async with conn.transaction():
            task = await store.next_task(conn, request.path_params["project"])
            if task and task["status"] == "pending":
                task = await rules.transition(conn, task["id"], "playing", "runner")
    return _json({"task": task})


async def task_transition(request: Request) -> JSONResponse:
    """Runner-side moves only; human moves exist in the UI, never here."""
    body = await request.json()
    doc_id = int(request.path_params["doc_id"])
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        async with conn.transaction():
            try:
                task = await rules.transition(conn, doc_id, body["to"], "runner")
            except rules.Forbidden as e:
                return _json({"error": str(e)}, 409)
            if body.get("run"):
                runs = task["body"].get("runs", []) + [body["run"]]
                await store.add_version(conn, None, doc_id, {**task["body"], "runs": runs})
            if body["to"] == "done":
                await store.finish_plan_if_complete(conn, task["body"]["plan_id"])
    return _json({"status": body["to"]})


def routes() -> list:
    return [
        Route("/api/projects/{project}/tasks/claim", claim_task, methods=["POST"]),
        Route("/api/tasks/{doc_id:int}/transition", task_transition, methods=["POST"]),
        Route("/api/projects/{project}/briefing", get_briefing),
        Route("/api/projects/{project}/gate-config", get_gate_config),
        Route("/api/gate-runs", post_gate_runs, methods=["POST"]),
        Route("/api/gate-runs", get_gate_runs),
    ]
