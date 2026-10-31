"""HTTP API for hooks and the runner (the plugin's scripts are stdlib-only, so no MCP client)."""

import json

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import briefing, insights, plans, rules, services, store
from .gates import GateConfig


def jsonable(data):
    """Dates and other rich values as strings."""
    return json.loads(json.dumps(data, default=str))


def _json(data, status_code: int = 200) -> JSONResponse:
    return JSONResponse(jsonable(data), status_code=status_code)


async def gate_config_body(conn, project: str) -> dict:
    """The gate config, plus the gates approved plans will add once their setup task merges."""
    doc = await store.latest_of_type(conn, project, "gate_config")
    cfg = GateConfig.from_yaml(doc["body"]["yaml"]) if doc else GateConfig()
    return {"version": doc["version"] if doc else None, "config": cfg.model_dump(mode="json"),
            "pending": await store.pending_gates(conn, project)}


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
        # A feature whose tasks are all merged is tried in a running environment before anything else.
        if soundcheck := await plans.soundcheck_due(conn, request.path_params["project"]):
            return _json({"task": None, "rehearse": None, "soundcheck": soundcheck})
        async with conn.transaction():
            task = await store.next_task(conn, request.path_params["project"])
            if task and task["status"] == "pending":
                task = await rules.transition(conn, task["id"], "playing", "runner")
            plan = await store.get_document(conn, task["body"]["plan_id"]) if task else None
    # A wave plan whose turn has come is rehearsed against the code before its first task plays.
    rehearse = plan if task and task["status"] == "playing" and plans.needs_rehearsal(task, plan) else None
    return _json({"task": task, "rehearse": rehearse, "soundcheck": None})


async def post_rehearsal(request: Request) -> JSONResponse:
    try:
        r = plans.Rehearsal.model_validate(await request.json())
    except ValidationError as e:
        return _json({"error": "invalid_rehearsal", "detail": e.errors()}, 422)
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        try:
            outcome = await plans.rehearse(conn, indexer, int(request.path_params["doc_id"]), r)
        except rules.Forbidden as e:
            return _json({"error": str(e)}, 409)
    return _json({"plan_status": "held" if outcome == "held" else "approved"})


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
            if body.get("run") or (body["to"] == "done" and task["body"].get("playback")):
                new = {**task["body"], "runs": task["body"].get("runs", []) + ([body["run"]] if body.get("run") else [])}
                if body["to"] == "done" and task["body"].get("playback"):
                    new["playback_review"] = {"status": "pending"}  # merged; the human hears it when they like
                await store.add_version(conn, None, doc_id, new)
            if body["to"] == "done":
                # Gates a plan proposed (approved by the human with it) start once their tooling is merged.
                await plans.apply_gates(conn, indexer, task["project"], task["body"].get("adds_gates", []))
                if await store.finish_plan_if_complete(conn, task["body"]["plan_id"]):
                    await plans.finish_wave_if_complete(conn, task["body"]["plan_id"])
    return _json({"status": body["to"]})


async def feature_dossier(request: Request) -> JSONResponse:
    """Everything the scribe needs to (re)write a feature's living doc."""
    project, feature = request.path_params["project"], request.path_params["feature"]
    pool, _ = await services.get()
    async with pool.connection() as conn:
        def slim(docs, n):
            return [{"id": d["id"], "status": d["status"], "created_at": d["created_at"], "body": d["body"]}
                    for d in docs[:n]]
        return _json({
            "doc": await store.feature_doc(conn, project, feature),
            "handoffs": slim(await store.list_documents(conn, project, type="handoff", feature=feature), 10),
            "plans": slim(await store.list_documents(conn, project, type="plan", feature=feature), 5),
            "decisions": slim([d for d in await store.list_documents(conn, project, type="decision", feature=feature)
                               if d["status"] in ("accepted", "proposed")], 20),
        })


async def put_feature_doc(request: Request) -> JSONResponse:
    project = request.path_params["project"]
    feature = request.path_params.get("feature")  # absent: the system overview
    body = await request.json()
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        doc_id = await store.save_feature_doc(conn, indexer, project, feature, body["doc"],
                                              body.get("layers", []), body.get("commit_sha"))
    return _json({"document_id": doc_id})


async def get_overview(request: Request) -> JSONResponse:
    pool, _ = await services.get()
    async with pool.connection() as conn:
        return _json({"doc": await store.feature_doc(conn, request.path_params["project"], None)})


async def get_features(request: Request) -> JSONResponse:
    project = request.path_params["project"]
    pool, _ = await services.get()
    async with pool.connection() as conn:
        out = []
        for f in await store.features(conn, project):
            doc = await store.feature_doc(conn, project, f["slug"])
            out.append({**f, "overview": doc["body"].get("overview") if doc else None,
                        "kind": doc["body"].get("kind", "product") if doc else "product"})
    return _json(out)


async def get_insights(request: Request) -> JSONResponse:
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        return _json(await insights.build(conn, request.path_params["project"], indexer.embedder))


async def post_task(request: Request) -> JSONResponse:
    """Cues queue work here; it lands in the maintenance plan, which a human approves once."""
    body = await request.json()
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        result = await store.enqueue_task(
            conn, indexer, request.path_params["project"], title=body["title"], spec=body["spec"],
            feature=body.get("feature"), layers=body.get("layers", []), source=body["source"])
    return _json(result, 201)


async def post_soundcheck(request: Request) -> JSONResponse:
    try:
        r = plans.SoundcheckResult.model_validate(await request.json())
    except ValidationError as e:
        return _json({"error": "invalid_soundcheck", "detail": e.errors()}, 422)
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        try:
            outcome = await plans.record_soundcheck(conn, indexer, int(request.path_params["doc_id"]), r)
        except rules.Forbidden as e:
            return _json({"error": str(e)}, 409)
    return _json({"outcome": outcome})


async def untuned_waves(request: Request) -> JSONResponse:
    """Finished waves the tune loop has not looked at yet."""
    pool, _ = await services.get()
    async with pool.connection() as conn:
        waves = await store.list_documents(conn, request.path_params["project"], type="wave", status="done", limit=50)
    return _json([w for w in waves if "tuned" not in w["body"]])


async def wave_tuned(request: Request) -> JSONResponse:
    body = await request.json()
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        async with conn.transaction():
            wave = await store.get_document(conn, int(request.path_params["doc_id"]))
            if not wave or wave["type"] != "wave":
                return _json({"error": "not a wave"}, 404)
            await store.add_version(conn, None, wave["id"], {**wave["body"], "tuned": {
                "at": plans._now(), "summary": str(body.get("summary", ""))[:4000]}})
    return _json({"ok": True})


async def post_events(request: Request) -> JSONResponse:
    """The runner streams what its sessions are doing: {project, events: [{task_id?, plan_id?, role, kind, text}]}."""
    body = await request.json()
    pool, _ = await services.get()
    async with pool.connection() as conn:
        n = await store.add_events(conn, body["project"], body.get("events", []))
    return _json({"stored": n}, 201)


async def get_events(request: Request) -> JSONResponse:
    p = request.query_params
    pool, _ = await services.get()
    async with pool.connection() as conn:
        return _json(await store.events(conn, task_id=int(request.path_params["doc_id"]), after=int(p.get("after", 0))))


def routes() -> list:
    return [
        Route("/api/projects/{project}/insights", get_insights),
        Route("/api/projects/{project}/tasks", post_task, methods=["POST"]),
        Route("/api/projects/{project}/features", get_features),
        Route("/api/projects/{project}/features/{feature}/dossier", feature_dossier),
        Route("/api/projects/{project}/features/{feature}/doc", put_feature_doc, methods=["PUT"]),
        Route("/api/projects/{project}/overview", put_feature_doc, methods=["PUT"]),
        Route("/api/projects/{project}/overview", get_overview),
        Route("/api/projects/{project}/tasks/claim", claim_task, methods=["POST"]),
        Route("/api/tasks/{doc_id:int}/transition", task_transition, methods=["POST"]),
        Route("/api/plans/{doc_id:int}/rehearsal", post_rehearsal, methods=["POST"]),
        Route("/api/plans/{doc_id:int}/soundcheck", post_soundcheck, methods=["POST"]),
        Route("/api/projects/{project}/waves/untuned", untuned_waves),
        Route("/api/waves/{doc_id:int}/tuned", wave_tuned, methods=["POST"]),
        Route("/api/projects/{project}/briefing", get_briefing),
        Route("/api/projects/{project}/gate-config", get_gate_config),
        Route("/api/events", post_events, methods=["POST"]),
        Route("/api/tasks/{doc_id:int}/events", get_events),
        Route("/api/gate-runs", post_gate_runs, methods=["POST"]),
        Route("/api/gate-runs", get_gate_runs),
    ]
