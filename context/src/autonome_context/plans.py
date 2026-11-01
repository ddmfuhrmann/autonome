"""Plans, waves and their tasks. A plan is drafted by an agent with the human, approved only by the
human, and approval turns its tasks into the queue the runner plays. A wave groups the plans of
several features: the human approves them together, and they play in dependency order."""

import fnmatch
from datetime import UTC, datetime
from typing import Literal

import yaml
from psycopg import AsyncConnection
from pydantic import BaseModel, Field, model_validator

from . import backlog, rules, store
from .gates import Gate, GateConfig


class TaskSpec(BaseModel):
    title: str
    spec: str = Field(description="what to do, concretely enough to be done without asking. Markdown, laid out "
                                  "to be scanned, never one paragraph: a line on what changes, then **Where** "
                                  "(files, endpoints, entities, as a list) and **How** (the steps, as a list)")
    layers: list[str] = []
    acceptance: list[str] = Field(default=[], description="verifiable criteria, ideally checked by gates")
    depends_on: list[int] = Field(default=[], description="1-based positions of earlier tasks in this plan")


class ProposedGate(Gate):
    with_task: int | None = Field(
        default=None, description="1-based position of the task that sets this gate up; the gate joins the "
                                  "gate config when that task is merged. Omit if the tooling already exists.")


class ProposedConvention(BaseModel):
    title: str
    rule: str = Field(description="short and checkable, e.g. 'every API response uses a response model'")
    why: str = ""
    layers: list[str] = Field(default=[], description="the layers it applies to; empty for all")


class DesignOption(BaseModel):
    name: str = Field(description="short, e.g. 'repository per aggregate'")
    tradeoffs: str = Field(description="what it costs and what it buys, in one or two lines")


class DesignChoice(BaseModel):
    """A choice of approach or pattern the plan makes up front, so the implementers do not make it."""

    topic: str = Field(description="what is being decided, e.g. 'data access'")
    options: list[DesignOption] = Field(min_length=2, description="the options weighed, the chosen one included")
    chosen: str = Field(description="the name of the chosen option")
    why: str = Field(description="why this one, for this project now")

    @model_validator(mode="after")
    def _chosen_is_an_option(self):
        if self.chosen not in {o.name for o in self.options}:
            raise ValueError(f"chosen {self.chosen!r} is not one of the options of {self.topic!r}")
        return self


class Turn(BaseModel):
    role: str
    text: str


class PlanBody(BaseModel):
    title: str
    goal: str = Field(description="one or two sentences: what changes, and for whom")
    context: str = Field(default="", description="Markdown, laid out to be scanned: short sections under bold "
                                                 "labels, lists over paragraphs")
    scope: list[str] = []
    out_of_scope: list[str] = []
    acceptance: list[str] = []
    risks: list[str] = []
    tasks: list[TaskSpec]
    gates: list[ProposedGate] = Field(
        default=[], description="gates this plan needs that the project's gate config lacks; approving the "
                                "plan adds them")
    conventions: list[ProposedConvention] = Field(
        default=[], description="rules for layers the project has none (or nothing relevant) for; approving "
                                "the plan makes them active, before its tasks play")
    design: list[DesignChoice] = Field(
        default=[], description="choices of approach or pattern the plan makes, each with the options weighed; "
                                "the tasks follow the chosen option, and going another way is a deviation")
    discussion: list[Turn] = Field(default=[], description="the questions and answers that shaped the plan")
    backlog_items: list[str] = Field(
        default=[], description="ids of the backlog items this plan delivers (e.g. w2-ledger); approving the plan "
                                "marks them doing, finishing it marks them done, abandoning it puts them back to next")


async def mark_backlog(conn: AsyncConnection, plan: dict, status: str) -> None:
    """Keeps the backlog in step with the plans that deliver it. A plan that ends done closes its items;
    one abandoned puts them back. An item already done stays done."""
    for key in plan["body"].get("backlog_items") or []:
        try:
            doc, i = await backlog._locate(conn, plan["project"], key)
        except rules.Forbidden:
            continue  # removed from the backlog since the plan was written
        if doc["body"]["items"][i].get("status") == "done" and status != "done":
            continue
        await backlog.update_item(conn, None, plan["project"], key, by=f"plan #{plan['id']}", status=status)


class WaveFeature(BaseModel):
    plan_id: int = Field(description="a draft plan of this project")
    depends_on: list[int] = Field(default=[], description="1-based positions of earlier features in this wave")


class WaveBody(BaseModel):
    title: str
    goal: str
    out_of_scope: list[str] = []
    features: list[WaveFeature] = Field(description="in the order they should be played")
    discussion: list[Turn] = Field(default=[], description="the questions and answers that shaped the wave")


class Rehearsal(BaseModel):
    """The runner's check of a wave plan against the code as it is when its turn comes."""

    verdict: Literal["ready", "revise"]
    notes: str = Field(default="", description="what the implementers should know about the code as it is now")
    plan: PlanBody | None = Field(default=None, description="the revised plan, when the verdict is revise")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


async def draft(
    conn: AsyncConnection, indexer: store.Indexer, project: str, feature: str | None, layers: list[str],
    body: PlanBody, plan_id: int | None = None,
) -> int:
    """Creates a draft plan, or revises one that is still a draft."""
    data = body.model_dump(mode="json")
    for key in body.backlog_items:
        await backlog._locate(conn, project, key)  # an unknown id answers with close matches
    async with conn.transaction():
        if plan_id is None:
            return await store.create_document(conn, indexer, project=project, type="plan", body=data,
                                               feature=feature, layers=layers, status="draft")
        doc = await store.get_document(conn, plan_id)
        if not doc or doc["type"] != "plan" or doc["project"] != project:
            raise rules.Forbidden(f"plan {plan_id} does not exist in {project}")
        if doc["status"] != "draft":
            raise rules.Forbidden(f"plan {plan_id} is {doc['status']}; only drafts can be revised")
        await store.add_version(conn, indexer, plan_id, data)
        return plan_id


async def apply_gates(conn: AsyncConnection, indexer: store.Indexer | None, project: str, gates: list[dict]) -> None:
    """Adds gates (by name, never replacing one) to the project's gate config. Only called on behalf of
    a human: the approval of the plan that proposed them. Comments in the YAML do not survive."""
    doc = await store.latest_of_type(conn, project, "gate_config")
    cfg = (yaml.safe_load(doc["body"]["yaml"]) if doc else None) or {}
    names = {g.get("name") for g in cfg.get("gates", [])}
    new = [Gate.model_validate(g).model_dump(mode="json", exclude_defaults=True)
           for g in gates if g["name"] not in names]
    if not new:
        return
    cfg["gates"] = cfg.get("gates", []) + new
    text = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
    GateConfig.from_yaml(text)
    await store.save_singleton(conn, indexer, project, "gate_config", {"yaml": text})


async def apply_conventions(conn: AsyncConnection, indexer: store.Indexer | None, project: str,
                            conventions: list[ProposedConvention]) -> None:
    """Makes a plan's proposed conventions active, skipping any whose title is already active. Only
    on behalf of a human: the approval of the plan. It never changes an existing convention."""
    active = {c["body"].get("title") for c in await store.list_documents(conn, project, type="convention",
                                                                         status="active", limit=500)}
    for c in conventions:
        if c.title not in active:
            await store.create_document(conn, indexer, project=project, type="convention", status="active",
                                        layers=c.layers, body={"title": c.title, "rule": c.rule, "why": c.why})


async def _queue_tasks(
    conn: AsyncConnection, indexer: store.Indexer, plan: dict, body: PlanBody, *,
    after_plans: list[int], wave_id: int | None,
) -> list[int]:
    gate_doc = await store.latest_of_type(conn, plan["project"], "gate_config")
    fermata, playback = await store.review_flags(conn, plan["project"], plan["feature"])
    gates = [g.model_dump(mode="json") for g in body.gates]
    await apply_gates(conn, indexer, plan["project"], [g for g in gates if not g["with_task"]])
    await apply_conventions(conn, indexer, plan["project"], body.conventions)
    task_ids: list[int] = []
    for seq, t in enumerate(body.tasks, start=1):
        task = {"title": t.title, "spec": t.spec, "acceptance": t.acceptance, "plan_id": plan["id"],
                "seq": seq, "depends_on": [task_ids[i - 1] for i in t.depends_on if 0 < i < seq],
                "fermata": fermata, "playback": playback}
        if after_plans:
            task["after_plans"] = after_plans
        if wave_id:
            task["wave_id"] = wave_id
        if adds := [g for g in gates if g["with_task"] == seq]:
            task["adds_gates"] = adds
        task_id = await store.create_document(conn, indexer, project=plan["project"], type="task",
                                              status="pending", feature=plan["feature"],
                                              layers=t.layers or plan["layers"], body=task)
        await store.link(conn, plan["id"], task_id, "has_task")
        task_ids.append(task_id)
    return task_ids


async def wave_of(conn: AsyncConnection, plan: dict) -> dict | None:
    """The live (draft or approved) wave a plan belongs to."""
    for w in await store.list_documents(conn, plan["project"], type="wave", limit=200):
        if w["status"] in ("draft", "approved") and any(f["plan_id"] == plan["id"] for f in w["body"]["features"]):
            return w
    return None


async def approve(conn: AsyncConnection, indexer: store.Indexer, plan_id: int) -> list[int]:
    """Human-only. Approves a plan on its own and queues its tasks in order."""
    async with conn.transaction():
        plan = await store.get_document(conn, plan_id)
        if plan and plan["type"] == "plan" and (wave := await wave_of(conn, plan)):
            raise rules.Forbidden(f"plan {plan_id} belongs to wave #{wave['id']}; approve the wave")
        plan = await rules.transition(conn, plan_id, "approved", "human")
        await mark_backlog(conn, plan, "doing")
        return await _queue_tasks(conn, indexer, plan, PlanBody.model_validate(plan["body"]),
                                  after_plans=[], wave_id=None)


async def draft_wave(
    conn: AsyncConnection, indexer: store.Indexer, project: str, body: WaveBody, wave_id: int | None = None,
) -> int:
    """Creates a draft wave over draft plans, or revises one that is still a draft."""
    async with conn.transaction():
        if wave_id is not None:
            doc = await store.get_document(conn, wave_id)
            if not doc or doc["type"] != "wave" or doc["project"] != project or doc["status"] != "draft":
                raise rules.Forbidden(f"wave {wave_id} is not a draft wave of {project}")
        if not body.features:
            raise rules.Forbidden("a wave needs at least one feature")
        for pos, f in enumerate(body.features, start=1):
            plan = await store.get_document(conn, f.plan_id)
            if not plan or plan["type"] != "plan" or plan["project"] != project or plan["status"] != "draft":
                raise rules.Forbidden(f"feature {pos}: plan {f.plan_id} is not a draft plan of {project}")
            other = await wave_of(conn, plan)
            if other and other["id"] != wave_id:
                raise rules.Forbidden(f"plan {f.plan_id} already belongs to wave #{other['id']}")
            if any(not 0 < d < pos for d in f.depends_on):
                raise rules.Forbidden(f"feature {pos}: depends_on may only name earlier features")
        data = body.model_dump(mode="json")
        if wave_id is None:
            return await store.create_document(conn, indexer, project=project, type="wave", body=data,
                                               status="draft")
        await store.add_version(conn, indexer, wave_id, data)
        return wave_id


async def approve_wave(conn: AsyncConnection, indexer: store.Indexer, wave_id: int) -> dict[int, list[int]]:
    """Human-only. Approves the wave and every plan in it. A plan waits for the plans of the features it
    depends on to be done, and is rehearsed against the code before it plays."""
    async with conn.transaction():
        wave = await rules.transition(conn, wave_id, "approved", "human")
        body = WaveBody.model_validate(wave["body"])
        plan_ids = [f.plan_id for f in body.features]
        queued: dict[int, list[int]] = {}
        for f in body.features:
            plan = await rules.transition(conn, f.plan_id, "approved", "human")
            await mark_backlog(conn, plan, "doing")
            await store.link(conn, wave_id, f.plan_id, "has_plan")
            queued[f.plan_id] = await _queue_tasks(
                conn, indexer, plan, PlanBody.model_validate(plan["body"]),
                after_plans=[plan_ids[d - 1] for d in f.depends_on], wave_id=wave_id)
    return queued


def needs_rehearsal(task: dict, plan: dict) -> bool:
    """A wave plan that waited for other features is checked against the code before its first task."""
    return bool(task["body"].get("after_plans")) and "rehearsal" not in plan["body"]


async def rehearse(conn: AsyncConnection, indexer: store.Indexer, plan_id: int, r: Rehearsal) -> str:
    """Runner. Records the rehearsal; a plan that needs revising is held until the human decides."""
    async with conn.transaction():
        plan = await store.get_document(conn, plan_id)
        if not plan or plan["type"] != "plan" or plan["status"] != "approved":
            raise rules.Forbidden(f"plan {plan_id} is not an approved plan")
        if r.verdict == "revise" and r.plan is None:
            raise rules.Forbidden("a revise verdict needs the revised plan")
        await store.add_version(conn, indexer, plan_id, {**plan["body"], "rehearsal": {
            "at": _now(), **r.model_dump(mode="json", exclude_none=True)}})
        if r.verdict == "revise":
            await rules.transition(conn, plan_id, "held", "runner")
            return "held"
    return "ready"


async def resolve_rehearsal(conn: AsyncConnection, indexer: store.Indexer, plan_id: int, accept: bool) -> None:
    """Human-only. Accepts the revised plan (its tasks replace the unplayed old ones) or keeps the original."""
    async with conn.transaction():
        plan = await rules.transition(conn, plan_id, "approved", "human")
        rehearsal = plan["body"].get("rehearsal", {})
        if not accept or not rehearsal.get("plan"):
            await store.add_version(conn, indexer, plan_id, {
                **plan["body"], "rehearsal": {**rehearsal, "verdict": "kept", "plan": None}})
            return
        old = [await store.get_document(conn, l["id"]) for l in await store.links_of(conn, plan_id)
               if l["kind"] == "has_task" and l["direction"] == "out"]
        if any(t["status"] not in ("pending", "abandoned") for t in old):
            raise rules.Forbidden("tasks of this plan already started; the revision cannot replace them")
        for t in old:
            if t["status"] == "pending":
                await rules.transition(conn, t["id"], "abandoned", "human")
        original = PlanBody.model_validate(plan["body"]).model_dump(mode="json")
        revised = PlanBody.model_validate(rehearsal["plan"])
        # A revision changes the tasks, not what was decided: the backlog items and design stay with the plan.
        revised.backlog_items = revised.backlog_items or original.get("backlog_items", [])
        revised.design = revised.design or [DesignChoice.model_validate(d) for d in original.get("design", [])]
        await store.add_version(conn, indexer, plan_id, {
            **revised.model_dump(mode="json"),
            "rehearsal": {**rehearsal, "verdict": "revised", "plan": None, "original": original}})
        first = old[0]["body"] if old else {}
        await _queue_tasks(conn, indexer, plan, revised,
                           after_plans=first.get("after_plans", []), wave_id=first.get("wave_id"))


async def finish_wave_if_complete(conn: AsyncConnection, plan_id: int) -> None:
    plan = await store.get_document(conn, plan_id)
    if not plan or plan["status"] != "done":
        return
    await mark_backlog(conn, plan, "done")
    for w in await store.list_documents(conn, plan["project"], type="wave", status="approved", limit=200):
        ids = [f["plan_id"] for f in w["body"]["features"]]
        statuses = [(await store.get_document(conn, i))["status"] for i in ids] if plan_id in ids else []
        if statuses and all(s in ("done", "abandoned") for s in statuses):
            await rules.transition(conn, w["id"], "done", "runner")


class Fix(BaseModel):
    title: str
    spec: str = Field(description="what is wrong, how to reproduce it on the running app, and what to change")
    acceptance: list[str] = Field(default=[], description="ideally: a test that reproduces the failure now passes")


class CheckRun(BaseModel):
    name: str
    passed: bool
    output: str = ""


class SoundcheckResult(BaseModel):
    """The runner's try of a finished feature in a running environment."""

    passed: bool
    report: str = Field(description="what was exercised and what happened")
    checks: list[CheckRun] = []
    fixes: list[Fix] = Field(default=[], description="tasks that would make it pass, when it did not")


def _rounds(plan: dict) -> list[dict]:
    """Soundchecks since the last human retry."""
    runs = plan["body"].get("soundchecks", [])
    since = max((i for i, r in enumerate(runs) if r.get("retry")), default=-1)
    return [r for r in runs[since + 1:] if not r.get("retry")]


async def soundcheck_due(conn: AsyncConnection, project: str) -> dict | None:
    """The oldest approved plan whose tasks are all finished but whose feature has not passed a soundcheck."""
    for plan in reversed(await store.list_documents(conn, project, type="plan", status="approved", limit=200)):
        if plan["body"].get("kind") == "maintenance":
            continue
        tasks = [l for l in await store.links_of(conn, plan["id"]) if l["kind"] == "has_task" and l["direction"] == "out"]
        if tasks and all(t["status"] in ("done", "abandoned") for t in tasks) \
                and any(t["status"] == "done" for t in tasks) and await store.awaits_soundcheck(conn, plan):
            return plan
    return None


async def record_soundcheck(conn: AsyncConnection, indexer: store.Indexer, plan_id: int, r: SoundcheckResult) -> str:
    """Runner. A pass finishes the plan; a failure queues fix tasks in it, or blocks it once the fix rounds
    are spent (or there is nothing concrete to fix)."""
    async with conn.transaction():
        plan = await store.get_document(conn, plan_id)
        if not plan or plan["type"] != "plan" or plan["status"] != "approved":
            raise rules.Forbidden(f"plan {plan_id} is not an approved plan")
        runs = plan["body"].get("soundchecks", []) + [{"at": _now(), **r.model_dump(mode="json")}]
        await store.add_version(conn, indexer, plan_id, {**plan["body"], "soundchecks": runs})
        plan = await store.get_document(conn, plan_id)
        if r.passed:
            await store.finish_plan_if_complete(conn, plan_id)
            await finish_wave_if_complete(conn, plan_id)
            return "done"
        doc = await store.latest_of_type(conn, plan["project"], "gate_config")
        max_fixes = GateConfig.from_yaml(doc["body"]["yaml"]).soundcheck.max_fixes if doc else 0
        if not r.fixes or len(_rounds(plan)) > max_fixes:
            await rules.transition(conn, plan_id, "blocked", "runner")
            return "blocked"
        old = [await store.get_document(conn, l["id"]) for l in await store.links_of(conn, plan_id)
               if l["kind"] == "has_task" and l["direction"] == "out"]
        last = max(old, key=lambda t: t["body"]["seq"])
        for i, fix in enumerate(r.fixes, start=1):
            task_id = await store.create_document(
                conn, indexer, project=plan["project"], type="task", status="pending", feature=plan["feature"],
                layers=last["layers"],
                body={"title": fix.title, "spec": fix.spec, "acceptance": fix.acceptance, "plan_id": plan_id,
                      "seq": last["body"]["seq"] + i, "depends_on": [], "fermata": last["body"].get("fermata", False),
                      "playback": last["body"].get("playback", False),
                      "source": "soundcheck"} | ({"wave_id": last["body"]["wave_id"]} if last["body"].get("wave_id") else {}))
            await store.link(conn, plan_id, task_id, "has_task")
    return "fixing"


async def retry_soundcheck(conn: AsyncConnection, indexer: store.Indexer, plan_id: int) -> None:
    """Human-only. A blocked plan gets one more soundcheck with fresh fix rounds."""
    async with conn.transaction():
        plan = await rules.transition(conn, plan_id, "approved", "human")
        runs = plan["body"].get("soundchecks", []) + [{"at": _now(), "retry": True}]
        await store.add_version(conn, indexer, plan_id, {**plan["body"], "soundchecks": runs})


async def accept_as_is(conn: AsyncConnection, plan_id: int) -> None:
    """Human-only. A blocked plan is done as it is; the features after it may play."""
    async with conn.transaction():
        await rules.transition(conn, plan_id, "done", "human")
        await finish_wave_if_complete(conn, plan_id)


def plan_summary(plan: dict) -> str:
    """What the human approves, written from the stored plan, never from the agent's words."""
    b = PlanBody.model_validate(plan["body"])
    lines = [f"Plan #{plan['id']} {b.title}" + (f" (feature {plan['feature']})" if plan["feature"] else ""),
             f"Goal: {b.goal}", f"{len(b.tasks)} tasks:"]
    lines += [f"  {i}. {t.title}" for i, t in enumerate(b.tasks, start=1)]
    if b.out_of_scope:
        lines.append("Out of scope: " + "; ".join(b.out_of_scope))
    if b.design:
        lines.append("Design: " + "; ".join(
            f"{d.topic}: {d.chosen} (over {', '.join(o.name for o in d.options if o.name != d.chosen)})"
            for d in b.design))
    if b.conventions:
        lines.append("Adds conventions: " + "; ".join(
            f"{c.title} ({', '.join(c.layers) or 'all layers'}): {c.rule}" for c in b.conventions))
    if b.gates:
        lines.append("Adds gates: " + "; ".join(
            f"{g.name} (`{g.run}`" + (f", once task {g.with_task} merges)" if g.with_task else ")") for g in b.gates))
    return "\n".join(lines)


def wave_summary(wave: dict, wave_plans: list[dict]) -> str:
    body = WaveBody.model_validate(wave["body"])
    lines = [f"Wave #{wave['id']} {body.title}", f"Goal: {body.goal}", "Features, in order:"]
    for i, (f, p) in enumerate(zip(body.features, wave_plans), start=1):
        pb = PlanBody.model_validate(p["body"])
        after = f" (after {', '.join(map(str, f.depends_on))})" if f.depends_on else ""
        gates = f"; adds gates {', '.join(g.name for g in pb.gates)}" if pb.gates else ""
        gates += f"; adds conventions {', '.join(c.title for c in pb.conventions)}" if pb.conventions else ""
        lines.append(f"  {i}. {pb.title}: {len(pb.tasks)} tasks{after}{gates}")
    if body.out_of_scope:
        lines.append("Out of scope: " + "; ".join(body.out_of_scope))
    return "\n".join(lines)


async def reject_decision(conn: AsyncConnection, indexer: store.Indexer, decision_id: int,
                          actor: rules.Actor = "human") -> dict:
    """The human, or the runner with a triage verdict. A rejected deviation is undone, not just
    remembered: a task to change it goes into the project's maintenance plan."""
    async with conn.transaction():
        d = await rules.transition(conn, decision_id, "rejected", actor)
        handoffs = [l["id"] for l in await store.links_of(conn, decision_id) if l["kind"] == "records"]
        b = d["body"]
        spec = (f"The human rejected this choice, made in an earlier session: {b.get('title')}.\n\n"
                f"What was done and why: {b.get('decision', '')}\n\n"
                f"Change the code so it no longer does this, following the plan and the conventions that "
                f"apply. Decision #{decision_id}" + (f", recorded by handoff #{handoffs[0]}." if handoffs else "."))
        return await store.enqueue_task(conn, indexer, d["project"], title=f"Undo: {b.get('title')}", spec=spec,
                                        feature=d["feature"], layers=d["layers"], source=f"decision:{decision_id}")


def loose_ends(run: dict | None) -> list[str]:
    """What a merged run left open: reviewer findings of medium severity (high ones never merge).
    They do not block; they wait for the human's review instead of vanishing."""
    run = run or {}
    review = (run.get("reviews") or [run.get("review") or {}])[-1]
    return [f"review, {f['severity']} {f.get('kind', '')}: " + " ".join(str(f.get("issue", "")).split())
            for f in review.get("findings", []) if f.get("severity") in ("medium", "high")]


def unverified(items: list[dict]) -> list[str]:
    """Plan items a handoff reports as not fully done: partial or skipped."""
    return [f"handoff, {i['status']}: {i['item']}" + (f" ({i['note']})" if i.get("note") else "")
            for i in items if i.get("status") in ("partial", "skipped")]


def with_loose_ends(body: dict, ends: list[str]) -> dict:
    """The task body with these loose ends added to its playback review, opened when needed."""
    if not ends:
        return body
    review = dict(body.get("playback_review") or {})
    if review.get("status") != "pending":
        review = {"status": "pending"}
    seen = review.get("loose_ends", [])
    return {**body, "playback_review": {**review, "loose_ends": seen + [e for e in ends if e not in seen]}}


async def note_handoff(conn: AsyncConnection, indexer: store.Indexer | None, task_id: int, items: list[dict]) -> None:
    """A handoff arrives after the merge: its unverified items join the task's playback review."""
    task = await store.get_document(conn, task_id)
    ends = unverified(items)
    if task and task["type"] == "task" and task["status"] == "done" and ends:
        await store.add_version(conn, indexer, task_id, with_loose_ends(task["body"], ends))


async def playback_heard(conn: AsyncConnection, indexer: store.Indexer, task_id: int) -> dict:
    """Human-only. The merged work of a playback feature was reviewed and is fine."""
    async with conn.transaction():
        task = await _playback_task(conn, task_id)
        await store.add_version(conn, indexer, task_id, {**task["body"], "playback_review": {"status": "heard", "at": _now()}})
    return {"status": "heard"}


async def playback_fix(conn: AsyncConnection, indexer: store.Indexer, task_id: int, reason: str) -> dict:
    """Human-only. Review forward: what needs fixing in merged work becomes a new task, never a block."""
    reason = reason.strip()
    if not reason:
        raise rules.Forbidden("say what needs fixing: it becomes the fix task's instruction")
    async with conn.transaction():
        task = await _playback_task(conn, task_id)
        b = task["body"]
        commit = ((b.get("runs") or [{}])[-1]).get("merged_sha", "")
        spec = (f"In review after merge, the human asked for a fix to task #{task_id} ({b['title']})"
                + (f", merged as {commit[:10]}" if commit else "") + f":\n\n{reason}\n\n"
                f"The original task, for context: {b.get('spec', '')}")
        fix = await store.enqueue_task(conn, indexer, task["project"], title=f"Fix: {b['title']}", spec=spec,
                                       feature=task["feature"], layers=task["layers"], source=f"playback:{task_id}")
        await store.add_version(conn, indexer, task_id, {**b, "playback_review": {
            "status": "fix", "at": _now(), "reason": reason, "fix_task_id": fix["task_id"]}})
    return {"status": "fix", **fix}


async def _playback_task(conn: AsyncConnection, task_id: int) -> dict:
    task = await store.get_document(conn, task_id)
    if not task or task["type"] != "task" or (task["body"].get("playback_review") or {}).get("status") != "pending":
        raise rules.Forbidden(f"task {task_id} has no playback waiting")
    return task
