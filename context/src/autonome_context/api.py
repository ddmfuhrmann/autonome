"""HTTP API for hooks and the runner (the plugin's scripts are stdlib-only, so no MCP client)."""

import json
import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, ValidationError
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


async def post_spend(request: Request) -> JSONResponse:
    """`{project, role, cost_usd, tokens?, task_id?, plan_id?, feature?}`: one agent session's cost."""
    b = await request.json()
    if not b.get("project") or not b.get("role") or not isinstance(b.get("cost_usd"), (int, float)):
        return _json({"error": "project, role and a numeric cost_usd are required"}, 400)
    pool, _ = await services.get()
    async with pool.connection() as conn:
        await store.record_spend(conn, b["project"], b["role"], float(b["cost_usd"]), tokens=b.get("tokens"),
                                 task_id=b.get("task_id"), plan_id=b.get("plan_id"), feature=b.get("feature"))
    return _json({"recorded": 1}, 201)


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


_NO_CONTROL = r"^[^\x00-\x1f\x7f]*$"


async def claim_task(request: Request) -> JSONResponse:
    """The runner asks for work. A pending task is moved to running; a released one is returned as is."""
    pool, _ = await services.get()
    async with pool.connection() as conn:
        if paused := await store.project_pause(conn, request.path_params["project"]):
            return _json({"task": None, "preflight": None, "smoke_test": None, "paused": paused})
        # Plans left approved with every task finished (done or abandoned) are closed before looking for work.
        async with conn.transaction():
            for plan_id in await store.finish_complete_plans(conn, request.path_params["project"]):
                await plans.finish_wave_if_complete(conn, plan_id)
        # A feature whose tasks are all merged is tried in a running environment before anything else.
        if smoke_test := await plans.smoke_test_due(conn, request.path_params["project"]):
            return _json({"task": None, "preflight": None, "smoke_test": smoke_test})
        async with conn.transaction():
            task = await store.next_task(conn, request.path_params["project"])
            if task and task["status"] == "pending":
                task = await rules.transition(conn, task["id"], "running", "runner")
            plan = await store.get_document(conn, task["body"]["plan_id"]) if task else None
    # A wave plan whose turn has come is checked in preflight against the code before its first task plays.
    preflight = plan if task and task["status"] == "running" and plans.needs_preflight(task, plan) else None
    return _json({"task": task, "preflight": preflight, "smoke_test": None})


class Pause(BaseModel):
    by: Annotated[str, StringConstraints(min_length=1, max_length=80, pattern=_NO_CONTROL)]
    reason: Annotated[str, StringConstraints(max_length=500, pattern=_NO_CONTROL)] | None = None


async def post_pause(request: Request) -> JSONResponse:
    """Anyone may pause a project's queue; resuming is the human's, in the UI or through MCP."""
    try:
        p = Pause.model_validate(await request.json())
    except ValueError as e:  # malformed JSON, or a ValidationError (also a ValueError)
        detail = e.errors(include_context=False, include_input=False) if isinstance(e, ValidationError) else str(e)
        return _json({"error": "invalid_pause", "detail": detail}, 422)
    project = request.path_params["project"]
    pool, _ = await services.get()
    async with pool.connection() as conn:
        paused = await store.pause_project(conn, project, p.by, p.reason)
    if paused is None:
        return _json({"error": f"project {project!r} does not exist"}, 404)
    return _json({"paused": paused})


class RunnerStatus(BaseModel):
    """Only a boolean: the Jev key itself never reaches the context."""
    model_config = ConfigDict(extra="forbid")
    triage_key: StrictBool


async def post_runner_status(request: Request) -> JSONResponse:
    try:
        s = RunnerStatus.model_validate(await request.json())
    except ValueError as e:  # malformed JSON, or a ValidationError (also a ValueError)
        detail = e.errors(include_context=False, include_input=False) if isinstance(e, ValidationError) else str(e)
        return _json({"error": "invalid_runner_status", "detail": detail}, 422)
    project = request.path_params["project"]
    pool, _ = await services.get()
    async with pool.connection() as conn:
        found = await store.set_runner_status(conn, project, s.triage_key)
    if not found:
        return _json({"error": f"project {project!r} does not exist"}, 404)
    return _json({"ok": True})


async def post_preflight(request: Request) -> JSONResponse:
    try:
        r = plans.Preflight.model_validate(await request.json())
    except ValidationError as e:
        return _json({"error": "invalid_preflight", "detail": e.errors()}, 422)
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        try:
            outcome = await plans.preflight(conn, indexer, int(request.path_params["doc_id"]), r)
        except rules.Forbidden as e:
            return _json({"error": str(e)}, 409)
    return _json({"plan_status": "held" if outcome == "held" else "approved"})


class PrOpened(BaseModel):
    """The run that moves a task to in_pr: where the pull request is and who merges it."""
    model_config = ConfigDict(extra="allow")
    pr_url: Annotated[str, StringConstraints(max_length=500, pattern=r"^https://github\.com/[^\s\"'<>]+$")]
    pr_mode: Literal["manual", "auto"]


class SatisfiedTriage(BaseModel):
    """The Jev's verdict that closes a task as already satisfied; only an accept does."""
    model_config = ConfigDict(extra="allow")
    model: Annotated[str, StringConstraints(pattern=r"^jev-(latest|\d+(\.\d+)*)$")]
    answers: dict[Annotated[str, StringConstraints(min_length=1, max_length=80)],
                  Annotated[str, StringConstraints(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")]]
    verdict: Literal["accept"]


class AlreadySatisfied(BaseModel):
    """The run that moves a task to done without a merge: the commit in main that delivered it, and the Jev's accept."""
    model_config = ConfigDict(extra="allow")
    outcome: Literal["already_satisfied"]
    satisfied_by: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{7,40}$")]
    satisfied_triage: SatisfiedTriage


async def task_transition(request: Request) -> JSONResponse:
    """Runner-side moves only; human moves exist in the UI, never here."""
    body = await request.json()
    doc_id = int(request.path_params["doc_id"])
    if body.get("to") == "in_pr":
        try:
            PrOpened.model_validate(body.get("run") or {})
        except ValidationError as e:
            return _json({"error": "invalid_pr", "detail": e.errors(include_context=False, include_input=False)}, 422)
    satisfied = body.get("to") == "done" and (body.get("run") or {}).get("outcome") == "already_satisfied"
    if satisfied:
        try:
            AlreadySatisfied.model_validate(body["run"])
        except ValidationError as e:
            return _json({"error": "invalid_already_satisfied",
                          "detail": e.errors(include_context=False, include_input=False)}, 422)
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        async with conn.transaction():
            # Equal transitions at once wait here; the second sees the new status and is refused.
            await conn.execute("select 1 from document where id = %s for update", (doc_id,))
            current = await store.get_document(conn, doc_id)
            if current and current["status"] == "in_pr" and body.get("to") == "done" \
                    and not (body.get("run") or {}).get("merged_sha"):
                return _json({"error": "invalid_pr", "detail": "a merged pull request needs run.merged_sha"}, 422)
            if satisfied and current:
                cfg = (await gate_config_body(conn, current["project"]))["config"]
                if not cfg.get("triage") or cfg["triage"]["model"] != "jev-latest":
                    return _json({"error": "closing a task as already satisfied needs the triage on jev-latest"}, 403)
                if current["status"] != "running":
                    return _json({"error": "only a running task is closed as already satisfied"}, 409)
            try:
                task = await rules.transition(conn, doc_id, body["to"], "runner")
            except rules.Forbidden as e:
                return _json({"error": str(e)}, 409)
            # No diff, no review after merge and no loose ends: nothing was merged.
            ends = plans.loose_ends(body.get("run")) if body["to"] == "done" and not satisfied else []
            after_merge = body["to"] == "done" and not satisfied and task["body"].get("after_merge")
            if body.get("run") or after_merge:
                new = {**task["body"], "runs": task["body"].get("runs", []) + ([body["run"]] if body.get("run") else [])}
                if after_merge:
                    new["after_merge_review"] = {"status": "pending"}  # merged; the human hears it when they like
                # What the reviewer let through waits for the human too, instead of vanishing.
                await store.add_version(conn, None, doc_id, plans.with_loose_ends(new, ends))
            if body["to"] == "abandoned" and await store.finish_plan_if_complete(conn, task["body"]["plan_id"]):
                await plans.finish_wave_if_complete(conn, task["body"]["plan_id"])
            if body["to"] == "done":
                # Gates a plan proposed (approved by the human with it) start once their tooling is merged.
                await plans.apply_gates(conn, indexer, task["project"], task["body"].get("adds_gates", []),
                                        {"plan_id": task["body"]["plan_id"], "task_id": doc_id})
                if await store.finish_plan_if_complete(conn, task["body"]["plan_id"]):
                    await plans.finish_wave_if_complete(conn, task["body"]["plan_id"])
    return _json({"status": body["to"]})


_LINE = r"^[^\x00-\x1f\x7f]+$"
_TEXT = r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+$"  # multi-line: tab, newline and carriage return are allowed


class RevisedSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(min_length=1, max_length=20000, pattern=_LINE)]
    spec: Annotated[str, StringConstraints(min_length=1, max_length=20000, pattern=_TEXT)]
    acceptance: list[Annotated[str, StringConstraints(min_length=1, max_length=20000, pattern=_TEXT)]]


class StaffRevision(BaseModel):
    """The staff's revision of a running task's spec, with the Jev's verdict on whether it changes scope."""
    model_config = ConfigDict(extra="forbid")
    revised: RevisedSpec
    diagnosis: Annotated[str, StringConstraints(min_length=1, max_length=20000, pattern=_TEXT)]
    model: Annotated[str, StringConstraints(pattern=r"^jev-(latest|\d+(\.\d+)*)$")]
    answers: dict[Annotated[str, StringConstraints(min_length=1, max_length=80)],
                  Annotated[str, StringConstraints(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")]]
    verdict: Literal["accept", "hold"]


async def post_staff_revision(request: Request) -> JSONResponse:
    """`accept` swaps the running task's spec for the revised one; `hold` only records the proposal."""
    try:
        r = StaffRevision.model_validate(await request.json())
    except ValueError as e:  # malformed JSON, or a ValidationError (also a ValueError)
        detail = e.errors(include_context=False, include_input=False) if isinstance(e, ValidationError) else str(e)
        return _json({"error": "invalid_staff_revision", "detail": detail}, 422)
    doc_id = int(request.path_params["doc_id"])
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("select 1 from document where id = %s for update", (doc_id,))
            task = await store.get_document(conn, doc_id)
            if not task or task["type"] != "task":
                return _json({"error": f"task {doc_id} does not exist"}, 404)
            cfg = (await gate_config_body(conn, task["project"]))["config"]
            if not cfg.get("triage") or cfg["triage"]["model"] != "jev-latest":
                return _json({"error": "the staff's revision needs the triage on jev-latest"}, 403)
            if task["status"] != "running":
                return _json({"error": "only a running task has its spec revised"}, 403)
            b = task["body"]
            original = {"title": b["title"], "spec": b["spec"], "acceptance": b.get("acceptance", [])}
            revision = {"original": original, "diagnosis": r.diagnosis, "model": r.model, "answers": r.answers,
                        "verdict": r.verdict, "at": datetime.now(timezone.utc).isoformat()}
            new = {**b, "staff_revision": revision}
            if r.verdict == "accept":
                new.update(r.revised.model_dump())
            else:
                revision["revised"] = r.revised.model_dump()  # what the staff proposed, for the human
            await store.add_version(conn, indexer, doc_id, new)
    return _json({"status": "running", "verdict": r.verdict})


async def feature_dossier(request: Request) -> JSONResponse:
    """Everything the handoff writer needs to (re)write a feature's living doc."""
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
    description, paths = body.get("description"), body.get("paths")
    if description is not None and not isinstance(description, str):
        return _json({"error": "description must be a string"}, 400)
    if paths is not None and not (isinstance(paths, list) and all(isinstance(p, str) for p in paths)):
        return _json({"error": "paths must be a list of strings"}, 400)
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        try:
            async with conn.transaction():  # the doc and the catalog change together
                doc_id = await store.save_feature_doc(conn, indexer, project, feature, body["doc"],
                                                      body.get("layers", []), body.get("commit_sha"))
                if feature and ((description or "").strip() or paths is not None):
                    if not await store.update_feature_catalog(conn, indexer.embedder, project, feature,
                                                              description, paths):
                        raise LookupError(feature)
        except (LookupError, rules.Forbidden):  # Forbidden: save_feature_doc on a slug outside the catalog
            return _json({"error": f"feature {feature!r} is not in the catalog"}, 404)
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
        described = {c["slug"]: c for c in await store.catalog(conn, project)}
        for f in await store.features(conn, project):
            doc = await store.feature_doc(conn, project, f["slug"])
            entry = described.get(f["slug"], {})
            out.append({**f, "description": entry.get("description"), "paths": entry.get("paths") or [],
                        "overview": ((doc["body"].get("product") or {}).get("what_it_does")
                                     or doc["body"].get("overview")) if doc else None,
                        "kind": doc["body"].get("kind", "product") if doc else "product"})
    return _json(out)


async def get_insights(request: Request) -> JSONResponse:
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        return _json(await insights.build(conn, request.path_params["project"], indexer.embedder))


async def post_task(request: Request) -> JSONResponse:
    """Schedules queue work here; it lands in the maintenance plan, which a human approves once."""
    body = await request.json()
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        result = await store.enqueue_task(
            conn, indexer, request.path_params["project"], title=body["title"], spec=body["spec"],
            feature=body.get("feature"), layers=body.get("layers", []), source=body["source"])
    return _json(result, 201)


async def post_smoke_test(request: Request) -> JSONResponse:
    try:
        r = plans.SmokeTestResult.model_validate(await request.json())
    except ValidationError as e:
        return _json({"error": "invalid_smoke_test", "detail": e.errors()}, 422)
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        try:
            outcome = await plans.record_smoke_test(conn, indexer, int(request.path_params["doc_id"]), r)
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
    verdict: Literal["accept", "reject", "escalate"]
    round: int = Field(ge=1)
    # How much the deviation repeats a choice the human made on the feature (`human_choice_of` is that decision)
    repeats_human_choice: float | None = Field(default=None, ge=0, le=1)
    human_choice_of: int | None = None


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
            if d["body"].get("human_choice"):
                return _json({"error": "a human's choice is not triaged"}, 403)
            if d["status"] != "proposed" or not d["body"].get("deviation"):
                return _json({"error": "only a proposed deviation is triaged"}, 403)
            # A repeated human choice is accepted as such, whatever the other questions said: no undo.
            human = (v.repeats_human_choice is not None and v.repeats_human_choice >= cfg["triage"]["reject_at"]
                     and await _is_human_choice_of(conn, d, v.human_choice_of))
            if human:
                v = v.model_copy(update={"verdict": "accept"})
            exhausted = v.verdict == "reject" and v.round >= cfg["triage"]["max_rounds"]
            entry = {**v.model_dump(exclude_none=True), "at": plans._now()}
            body = {**d["body"], "triage": entry, "triage_history": d["body"].get("triage_history", []) + [entry]}
            if human:
                body["accepted_as_human_choice"] = v.human_choice_of or True
            if exhausted:
                body["triage_exhausted"] = True
            if v.verdict == "escalate":
                body["triage_conflict"] = True
            await store.add_version(conn, indexer, doc_id, body)
            if exhausted:
                return _json({"status": "proposed", "triage_exhausted": True})
            if v.verdict == "escalate":  # a rule conflict: the human fixes the rule; it waits in the queue, non-blocking
                return _json({"status": "proposed", "escalated": True})
            if v.verdict == "reject":
                await plans.reject_decision(conn, indexer, doc_id, "runner")
            else:
                await rules.transition(conn, doc_id, "accepted", "runner")
    return _json({"status": "rejected" if v.verdict == "reject" else "accepted"})


class RevisionVerdict(BaseModel):
    model: str
    state: str
    answers: dict[Annotated[str, StringConstraints(min_length=1, max_length=80)],
                  Annotated[str, StringConstraints(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")]]
    verdict: Literal["accept", "hold"]


_JEV = re.compile(r"jev-(latest|\d+(\.\d+)*)")


class _Refused(Exception):
    pass


async def post_preflight_triage(request: Request) -> JSONResponse:
    """The runner's classifier judged a preflight's revision: keep the verdict; accepting starts the plan."""
    try:
        v = RevisionVerdict.model_validate(await request.json())
    except ValueError as e:
        detail = e.errors(include_context=False, include_input=False) if isinstance(e, ValidationError) else str(e)
        return _json({"error": "invalid_triage", "detail": detail}, 422)
    plan_id = int(request.path_params["doc_id"])
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                # Concurrent verdicts on one plan wait here; the second sees the verdict and is refused.
                await conn.execute("select 1 from document where id = %s for update", (plan_id,))
                plan = await store.get_document(conn, plan_id)
                if not plan or plan["type"] != "plan":
                    return _json({"error": "not a plan"}, 404)
                cfg = (await gate_config_body(conn, plan["project"]))["config"]
                # The config asks for the alias; the verdict names the Jev that answered (`jev-1.14.0`).
                if not cfg.get("triage") or cfg["triage"]["model"] != "jev-latest" or not _JEV.fullmatch(v.model):
                    raise _Refused("only jev-latest judges revisions")
                r = plan["body"].get("preflight") or {}
                if plan["status"] != "held" or not r.get("plan"):
                    raise _Refused("only a held plan with a proposed revision is triaged")
                if r.get("triage"):
                    raise _Refused("this revision was already judged")
                triage = {"model": v.model, "answers": v.answers, "verdict": v.verdict, "at": plans._now()}
                await store.add_version(conn, indexer, plan_id, {**plan["body"], "preflight": {**r, "triage": triage}})
                if v.verdict == "accept":
                    await plans.resolve_preflight(conn, indexer, plan_id, accept=True, actor="runner")
        except (_Refused, rules.Forbidden) as e:  # the transaction rolled back: nothing changed
            return _json({"error": str(e)}, 403)
    return _json({"status": "approved" if v.verdict == "accept" else "held"})


def _revision_state(plan: dict) -> tuple[dict, bool]:
    """What the classifier is shown of a held plan's revision: only the tasks that changed, by position."""
    r = plan["body"]["preflight"]
    old, new = plan["body"].get("tasks") or [], r["plan"].get("tasks") or []

    def part(t: dict | None, i: int) -> dict | None:
        return {"position": i + 1, **{k: t.get(k) for k in ("title", "spec", "acceptance") if t.get(k)}} if t else None

    original, revised = [], []
    for i in range(max(len(old), len(new))):
        o = old[i] if i < len(old) else None
        n = new[i] if i < len(new) else None
        if any((o or {}).get(k) != (n or {}).get(k) for k in ("title", "spec", "acceptance")):
            original += [p for p in [part(o, i)] if p]
            revised += [p for p in [part(n, i)] if p]
    titles = [t.get("title") for t in old]
    shape_changed = len(old) != len(new) or any(
        t.get("title") in titles and titles.index(t.get("title")) != i for i, t in enumerate(new))
    state: dict = {}
    if plan_part := {k: plan["body"][k] for k in ("goal", "out_of_scope", "design") if plan["body"].get(k)}:
        state["plan"] = plan_part
    state["original"], state["revised"] = original, revised
    if r.get("notes"):
        state["notes"] = _cut_text(r["notes"], 3000)
    return state, shape_changed


def _cut_text(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n]


async def _pending_revisions(conn, project: str) -> list[dict]:
    """Held plans with a revision the classifier has yet to judge, oldest first."""
    out = []
    for p in await store.list_documents(conn, project, type="plan", status="held", limit=None, oldest_first=True):
        r = p["body"].get("preflight") or {}
        if not r.get("plan") or r.get("triage"):
            continue
        state, shape_changed = _revision_state(p)
        out.append({"plan_id": p["id"], "state": state, "shape_changed": shape_changed})
        if len(out) == 20:
            break
    return out


async def _decision_task(conn, decision_id: int) -> dict | None:
    """The task whose session made this decision: decision <-records- handoff -reports-> task."""
    for h in (l for l in await store.links_of(conn, decision_id) if l["kind"] == "records" and l["direction"] == "in"):
        for t in await store.links_of(conn, h["id"]):
            if t["kind"] == "reports" and t["direction"] == "out" and t["type"] == "task":
                return await store.get_document(conn, t["id"])
    return None


async def _triage_round(conn, task: dict | None) -> int:
    """1, or 1 + the round of decision Y when the task is the undo of Y (`source` is `decision:Y`).
    Counted from 1, as the verdict endpoint takes it (`TriageVerdict.round`): the first judgment is round 1."""
    seen: set[int] = set()
    rounds = 1
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
    if choices := await _human_choices(conn, d):
        state["human_choices"] = choices
    return state


def _judged_by_human(d: dict) -> bool:
    """A decision the human accepted or rejected (no runner verdict stands last on it), or born from the undo
    of a decision the human rejected."""
    if d["body"].get("undo_of_human_reject"):
        return True
    if not d["body"].get("deviation") or d["status"] not in ("accepted", "rejected"):
        return False
    last = (d["body"].get("triage_history") or [{}])[-1].get("verdict")
    return last != {"accepted": "accept", "rejected": "reject"}[d["status"]]


async def _human_choices(conn, d: dict, limit: int = 10) -> list[dict]:
    """The latest decisions of the same feature and project that the human judged: what a repeated choice is compared to."""
    if not d["feature"]:
        return []
    out = []
    for c in await store.list_documents(conn, d["project"], type="decision", feature=d["feature"], limit=None):
        if c["id"] != d["id"] and c["status"] in ("accepted", "rejected") and _judged_by_human(c):
            out.append({"id": c["id"], "title": c["body"].get("title"), "decision": c["body"].get("decision"),
                        "status": c["status"]})
            if len(out) == limit:
                break
    return out


async def _is_human_choice_of(conn, d: dict, choice_id: int | None) -> bool:
    """Whether `choice_id` is one of the human choices the state shows for this decision; any other claim of a
    repeated choice is ignored, so a feature without human choices behaves as it always did."""
    return choice_id is not None and any(c["id"] == choice_id for c in await _human_choices(conn, d))


async def get_triage(request: Request) -> JSONResponse:
    """Deviations the runner's classifier has yet to judge at their current round, oldest first."""
    project = request.path_params["project"]
    pool, _ = await services.get()
    out = []
    async with pool.connection() as conn:
        decisions = await store.list_documents(conn, project, type="decision", status="proposed", limit=None,
                                               oldest_first=True)
        for d in decisions:
            if (not d["body"].get("deviation") or d["body"].get("triage_exhausted") or d["body"].get("triage_conflict")
                    or d["body"].get("undo_of_human_reject") or d["body"].get("human_choice")):
                continue
            task =await _decision_task(conn, d["id"])
            round_ = await _triage_round(conn, task)
            if any(e.get("round") == round_ for e in d["body"].get("triage_history", [])):
                continue
            out.append({"decision_id": d["id"], "round": round_, "state": await _triage_state(conn, d, task)})
            if len(out) == 50:
                break
        screen = await _screen_pending(conn, project)
        revisions = await _pending_revisions(conn, project)
    return _json({"deviations": out, "screen": screen, "revisions": revisions})


async def _triage_since(conn, project: str) -> str | None:
    """When the project first had the triage on: the oldest gate config version whose triage model is not off."""
    async with conn.cursor() as cur:
        await cur.execute(
            "select dv.body, dv.created_at from document d join project p on p.id = d.project_id"
            " join document_version dv on dv.document_id = d.id"
            " where p.slug = %s and d.type = 'gate_config' order by dv.version", (project,))
        rows = await cur.fetchall()
    for body, at in rows:
        try:
            triage = GateConfig.from_yaml(body["yaml"]).triage
        except ValueError:  # an invalid old version never turned anything on
            continue
        if triage and triage.model != "off":
            return at
    return None


async def _screen_pending(conn, project: str) -> list[dict]:
    """Accepted non-deviations made since the triage was on, not yet screened, oldest first."""
    since = await _triage_since(conn, project)
    if since is None:
        return []
    out = []
    for d in await store.list_documents(conn, project, type="decision", status="accepted", limit=None,
                                        oldest_first=True):
        if (d["body"].get("deviation") or d["body"].get("screening") or d["body"].get("undo_of_human_reject")
                or d["body"].get("human_choice") or d["created_at"] <= since):
            continue
        out.append({"decision_id": d["id"], "state": await _triage_state(conn, d, await _decision_task(conn, d["id"]))})
        if len(out) == 50:
            break
    return out


class Screening(BaseModel):
    backend: str
    model: str
    is_deviation: float = Field(ge=0, le=1)
    reject_at: float = Field(gt=0, le=1)
    repeats_human_choice: float | None = Field(default=None, ge=0, le=1)
    human_choice_of: int | None = None


async def post_screen(request: Request) -> JSONResponse:
    """The runner's classifier screened an accepted decision: keep the result and, when it is really a
    deviation, reopen it as one so the triage judges it."""
    try:
        s = Screening.model_validate(await request.json())
    except ValueError as e:
        detail = e.errors(include_context=False, include_input=False) if isinstance(e, ValidationError) else str(e)
        return _json({"error": "invalid_screening", "detail": detail}, 422)
    doc_id = int(request.path_params["doc_id"])
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("select 1 from document where id = %s for update", (doc_id,))
            d = await store.get_document(conn, doc_id)
            if not d or d["type"] != "decision":
                return _json({"error": "not a decision"}, 404)
            cfg = (await gate_config_body(conn, d["project"]))["config"]
            if not cfg.get("triage") or cfg["triage"]["model"] == "off":
                return _json({"error": "triage is off for this project"}, 403)
            if d["body"].get("human_choice"):
                return _json({"error": "a human's choice is not screened"}, 403)
            if d["body"].get("screening"):
                return _json({"error": "already screened"}, 409)
            if d["status"] != "accepted" or d["body"].get("deviation"):
                return _json({"error": "only an accepted non-deviation is screened"}, 403)
            human = (s.repeats_human_choice is not None and s.repeats_human_choice >= s.reject_at
                     and await _is_human_choice_of(conn, d, s.human_choice_of))
            reopen = s.is_deviation >= s.reject_at and not human
            body = {**d["body"], "screening": {"backend": s.backend, "model": s.model,
                                               "is_deviation": s.is_deviation, "at": plans._now()}}
            if human:
                body["accepted_as_human_choice"] = s.human_choice_of or True
            if reopen:
                body |= {"deviation": True, "screened_as_deviation": True}
            await store.add_version(conn, indexer, doc_id, body)
            if reopen:
                await rules.transition(conn, doc_id, "proposed", "runner")
    return _json({"status": "proposed" if reopen else "accepted", "deviation": reopen})


async def waves_without_retro(request: Request) -> JSONResponse:
    """Finished waves the retro loop has not looked at yet."""
    pool, _ = await services.get()
    async with pool.connection() as conn:
        waves = await store.list_documents(conn, request.path_params["project"], type="wave", status="done", limit=50)
    return _json([w for w in waves if "retro_done" not in w["body"]])


async def wave_retro_done(request: Request) -> JSONResponse:
    body = await request.json()
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        async with conn.transaction():
            wave = await store.get_document(conn, int(request.path_params["doc_id"]))
            if not wave or wave["type"] != "wave":
                return _json({"error": "not a wave"}, 404)
            await store.add_version(conn, None, wave["id"], {**wave["body"], "retro_done": {
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


def _triage_hold_flag(plan: dict) -> str | None:
    """The question that weighed most when the classifier held a plan's revision, else None."""
    triage = (plan["body"].get("preflight") or {}).get("triage") or {}
    if triage.get("verdict") != "hold":
        return None

    def score(a):
        try:
            return float(a[1])
        except ValueError:
            return -1.0
    top = max((triage.get("answers") or {}).items(), key=score, default=None)
    return top[0] if top else "hold"


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
        paused = await store.project_pause(conn, project)
        triage = await _triage_on(conn, project)
        if triage:  # the queue leaves deviations to the classifier; the ones it gave up on still need a human
            queue += [{"kind": "decision", "doc": d} for d in await store.list_documents(conn, project, type="decision", status="proposed")
                      if d["body"].get("deviation") and d["body"].get("triage_exhausted")]
    last = lambda t: (t["body"].get("runs") or [{}])[-1]
    return _json({
        "tasks": [{"id": t["id"], "title": t["body"]["title"], "status": t["status"], "feature": t["feature"],
                   "plan_id": t["body"].get("plan_id"), "reason": (last(t).get("reason") or "")[:300],
                   "after_merge": (t["body"].get("after_merge_review") or {}).get("status"),
                   "satisfied_by": last(t).get("satisfied_by"),
                   "staff_revised": (t["body"].get("staff_revision") or {}).get("verdict") == "accept",
                   **(dict(zip(("pr_url", "pr_mode"), store.task_pr(t))) if t["status"] == "in_pr" else {})} for t in tasks],
        "plans": [{"id": p["id"], "title": p["body"].get("title"), "status": p["status"], "feature": p["feature"]}
                  for p in plans_],
        "waves": [{"id": w["id"], "title": w["body"].get("title"), "status": w["status"]} for w in waves],
        "waiting": [{"kind": i["kind"], "id": i["doc"]["id"], "title": i["doc"]["body"].get("title")
                     or i["doc"]["body"].get("summary", "")[:80], "blocks": i["kind"] in BLOCKING,
                     "deviation": bool(i["doc"]["body"].get("deviation")),
                     "exhausted": bool(i["doc"]["body"].get("triage_exhausted")),
                     "conflict": bool(i["doc"]["body"].get("triage_conflict")),
                     "pr_url": store.task_pr(i["doc"])[0] if i["kind"] == "in_pr" else None,
                     "triage_hold": _triage_hold_flag(i["doc"]) if i["kind"] == "held" else None} for i in queue],
        "triage": triage,
        "paused": paused,
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
        Route("/api/projects/{project}/pause", post_pause, methods=["POST"]),
        Route("/api/projects/{project}/runner-status", post_runner_status, methods=["POST"]),
        Route("/api/tasks/{doc_id:int}/transition", task_transition, methods=["POST"]),
        Route("/api/plans/{doc_id:int}/preflight", post_preflight, methods=["POST"]),
        Route("/api/plans/{doc_id:int}/smoke-test", post_smoke_test, methods=["POST"]),
        Route("/api/projects/{project}/waves/without-retro", waves_without_retro),
        Route("/api/projects/{project}/triage", get_triage),
        Route("/api/decisions/{doc_id:int}/triage", post_triage, methods=["POST"]),
        Route("/api/plans/{doc_id:int}/preflight-triage", post_preflight_triage, methods=["POST"]),
        Route("/api/decisions/{doc_id:int}/screen", post_screen, methods=["POST"]),
        Route("/api/waves/{doc_id:int}/retro-done", wave_retro_done, methods=["POST"]),
        Route("/api/projects/{project}/briefing", get_briefing),
        Route("/api/projects/{project}/gate-config", get_gate_config),
        Route("/api/events", post_events, methods=["POST"]),
        Route("/api/tasks/{doc_id:int}/events", get_events),
        Route("/api/tasks/{doc_id:int}/staff-revision", post_staff_revision, methods=["POST"]),
        Route("/api/gate-runs", post_gate_runs, methods=["POST"]),
        Route("/api/gate-runs", get_gate_runs),
        Route("/api/spend", post_spend, methods=["POST"]),
    ]
