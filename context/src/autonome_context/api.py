"""HTTP API for hooks and the runner (the plugin's scripts are stdlib-only, so no MCP client)."""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationError
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
            ends = plans.loose_ends(body.get("run")) if body["to"] == "done" else []
            if body.get("run") or (body["to"] == "done" and task["body"].get("playback")):
                new = {**task["body"], "runs": task["body"].get("runs", []) + ([body["run"]] if body.get("run") else [])}
                if body["to"] == "done" and task["body"].get("playback"):
                    new["playback_review"] = {"status": "pending"}  # merged; the human hears it when they like
                # What the reviewer let through waits for the human too, instead of vanishing.
                await store.add_version(conn, None, doc_id, plans.with_loose_ends(new, ends))
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


class TriageVerdict(BaseModel):
    backend: str
    model: str
    state: str
    # {question name: noul}; the values are the classifier's short answers, never free text
    answers: dict[Annotated[str, StringConstraints(min_length=1, max_length=80)],
                  Annotated[str, StringConstraints(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")]]
    verdict: Literal["accept", "reject"]
    round: int = Field(ge=1)


async def post_triage(request: Request) -> JSONResponse:
    """The runner's classifier judged a deviation: keep the verdict and move the decision with it."""
    try:
        v = TriageVerdict.model_validate(await request.json())
    except ValueError as e:  # malformed JSON, or a ValidationError (also a ValueError)
        detail = e.errors(include_context=False, include_input=False) if isinstance(e, ValidationError) else str(e)
        return _json({"error": "invalid_triage", "detail": detail}, 422)
    doc_id = int(request.path_params["doc_id"])
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        async with conn.transaction():
            # Concurrent verdicts on one decision wait here; the second sees the new status and is refused.
            await conn.execute("select 1 from document where id = %s for update", (doc_id,))
            d = await store.get_document(conn, doc_id)
            if not d or d["type"] != "decision":
                return _json({"error": "not a decision"}, 404)
            cfg = (await gate_config_body(conn, d["project"]))["config"]
            if not cfg.get("triage") or cfg["triage"]["model"] == "off":
                return _json({"error": "triage is off for this project"}, 403)
            if d["status"] != "proposed" or not d["body"].get("deviation"):
                return _json({"error": "only a proposed deviation is triaged"}, 403)
            exhausted = v.verdict == "reject" and v.round >= cfg["triage"]["max_rounds"]
            entry = {**v.model_dump(), "at": plans._now()}
            body = {**d["body"], "triage": entry, "triage_history": d["body"].get("triage_history", []) + [entry]}
            if exhausted:
                body["triage_exhausted"] = True
            await store.add_version(conn, indexer, doc_id, body)
            if exhausted:
                return _json({"status": "proposed", "triage_exhausted": True})
            if v.verdict == "reject":
                await plans.reject_decision(conn, indexer, doc_id, "runner")
            else:
                await rules.transition(conn, doc_id, "accepted", "runner")
    return _json({"status": "rejected" if v.verdict == "reject" else "accepted"})


async def _decision_task(conn, decision_id: int) -> dict | None:
    """The task whose session made this decision: decision <-records- handoff -reports-> task."""
    for h in (l for l in await store.links_of(conn, decision_id) if l["kind"] == "records" and l["direction"] == "in"):
        for t in await store.links_of(conn, h["id"]):
            if t["kind"] == "reports" and t["direction"] == "out" and t["type"] == "task":
                return await store.get_document(conn, t["id"])
    return None


async def _triage_round(conn, task: dict | None) -> int:
    """0, or 1 + the round of decision Y when the task is the undo of Y (`source` is `decision:Y`)."""
    seen: set[int] = set()
    rounds = 0
    while task and str(task["body"].get("source", "")).startswith("decision:"):
        try:
            y = int(task["body"]["source"].split(":", 1)[1])
        except ValueError:
            break
        if y in seen:
            break
        seen.add(y)
        rounds += 1
        task = await _decision_task(conn, y)
    return rounds


async def _triage_state(conn, d: dict, task: dict | None) -> dict:
    """What the classifier is shown; parts that do not exist are left out, never sent empty."""
    def pick(body: dict, keys: tuple[str, ...]) -> dict:
        return {k: body[k] for k in keys if body.get(k)}

    state: dict = {}
    if deviation := pick(d["body"], ("title", "decision")):
        state["deviation"] = deviation
    if task:
        tb = task["body"]
        if task_part := pick(tb, ("title", "spec", "acceptance")):
            state["task"] = task_part
        plan = await store.get_document(conn, tb["plan_id"]) if tb.get("plan_id") else None
        if plan and (plan_part := pick(plan["body"], ("goal", "out_of_scope"))):
            state["plan"] = plan_part
        runs = sorted(await store.gate_runs(conn, task_id=task["id"]), key=lambda r: r["id"])
        # The task's last run: the newest session's gates, the latest result of each.
        last: dict[str, bool] = {}
        for r in (r for r in runs if runs and r["session_id"] == runs[-1]["session_id"]):
            last[r["gate"]] = r["passed"]
        if last:
            state["gates"] = [{"name": n, "passed": p} for n, p in last.items()]
        findings = ((tb.get("runs") or [{}])[-1].get("review") or {}).get("findings") or []
        if findings:
            state["review"] = [{"severity": f.get("severity"), "kind": f.get("kind"), "issue": f.get("issue")}
                               for f in findings]
    convs = await store.conventions_for(conn, d["project"], d["layers"])
    if convs:
        state["conventions"] = [{"title": c["body"].get("title"), "rule": c["body"].get("rule")} for c in convs]
    return state


async def get_triage(request: Request) -> JSONResponse:
    """Deviations the runner's classifier has yet to judge at their current round, oldest first."""
    project = request.path_params["project"]
    pool, _ = await services.get()
    out = []
    async with pool.connection() as conn:
        decisions = await store.list_documents(conn, project, type="decision", status="proposed", limit=None,
                                               oldest_first=True)
        for d in decisions:
            if not d["body"].get("deviation") or d["body"].get("triage_exhausted"):
                continue
            task = await _decision_task(conn, d["id"])
            round_ = await _triage_round(conn, task)
            if any(e.get("round") == round_ for e in d["body"].get("triage_history", [])):
                continue
            out.append({"decision_id": d["id"], "round": round_, "state": await _triage_state(conn, d, task)})
            if len(out) == 50:
                break
    return _json(out)


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


async def project_state(request: Request) -> JSONResponse:
    """A compact snapshot for watchers (`autonome watch`): every task and plan with its status, the
    waves, and what waits on the human, split into what blocks and what can wait."""
    from .web import BLOCKING, _queue, _triage_on  # the same queue the Approvals page shows

    project = request.path_params["project"]
    pool, _ = await services.get()
    async with pool.connection() as conn:
        tasks = await store.list_documents(conn, project, type="task", limit=1000)
        plans_ = await store.list_documents(conn, project, type="plan", limit=300)
        waves = await store.list_documents(conn, project, type="wave", limit=50)
        queue = await _queue(conn, project)
        triage = await _triage_on(conn, project)
        if triage:  # the queue leaves deviations to the classifier; the ones it gave up on still need a human
            queue += [{"kind": "decision", "doc": d} for d in await store.list_documents(conn, project, type="decision", status="proposed")
                      if d["body"].get("deviation") and d["body"].get("triage_exhausted")]
    last = lambda t: (t["body"].get("runs") or [{}])[-1]
    return _json({
        "tasks": [{"id": t["id"], "title": t["body"]["title"], "status": t["status"], "feature": t["feature"],
                   "plan_id": t["body"].get("plan_id"), "reason": (last(t).get("reason") or "")[:300],
                   "playback": (t["body"].get("playback_review") or {}).get("status")} for t in tasks],
        "plans": [{"id": p["id"], "title": p["body"].get("title"), "status": p["status"], "feature": p["feature"]}
                  for p in plans_],
        "waves": [{"id": w["id"], "title": w["body"].get("title"), "status": w["status"]} for w in waves],
        "waiting": [{"kind": i["kind"], "id": i["doc"]["id"], "title": i["doc"]["body"].get("title")
                     or i["doc"]["body"].get("summary", "")[:80], "blocks": i["kind"] in BLOCKING,
                     "deviation": bool(i["doc"]["body"].get("deviation")),
                     "exhausted": bool(i["doc"]["body"].get("triage_exhausted"))} for i in queue],
        "triage": triage,
    })


def routes() -> list:
    return [
        Route("/api/projects/{project}/insights", get_insights),
        Route("/api/projects/{project}/state", project_state),
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
        Route("/api/projects/{project}/triage", get_triage),
        Route("/api/decisions/{doc_id:int}/triage", post_triage, methods=["POST"]),
        Route("/api/waves/{doc_id:int}/tuned", wave_tuned, methods=["POST"]),
        Route("/api/projects/{project}/briefing", get_briefing),
        Route("/api/projects/{project}/gate-config", get_gate_config),
        Route("/api/events", post_events, methods=["POST"]),
        Route("/api/tasks/{doc_id:int}/events", get_events),
        Route("/api/gate-runs", post_gate_runs, methods=["POST"]),
        Route("/api/gate-runs", get_gate_runs),
    ]
