"""Gate configuration: the orchestration of a project's gates. The rules themselves (linters,
tests, Semgrep) live in the project's repo; this only says what runs, in which order, and how."""

from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    """An unknown key is an error at any depth, so a renamed key is never ignored in silence."""

    model_config = ConfigDict(extra="forbid")


class Gate(_Strict):
    name: str
    run: str = Field(description="shell command, run from the repo root")
    mode: Literal["fix", "block"] = "block"
    fast: bool = Field(default=False, description="part of the inner loop the implementer runs while working")
    when: list[str] = Field(default=[], description="path globs; the gate runs only if a changed file matches")
    timeout_s: int = 900


class Retries(_Strict):
    max: int = 3
    stop_on_repeat: bool = True


class Budget(_Strict):
    task_tokens: int = 400_000
    task_minutes: int = 30
    plan_tokens: int = 3_000_000


class HoldBeforeMerge(_Strict):
    features: list[str] = []


class ReviewAfterMerge(_Strict):
    """Features reviewed after merge: their tasks merge as usual and wait for the human's review in the
    background ("review forward"); a fix becomes a new task, never a block."""

    features: list[str] = []


class Schedule(_Strict):
    name: str
    kind: Literal["command", "doc-drift", "main-red", "simplify"]
    every_hours: int = 24
    run: str | None = Field(default=None, description="for kind=command: non-empty output or failure enqueues a task")
    task: str | None = Field(default=None, description="title of the task to enqueue")
    max_tasks: int | None = Field(
        default=None, ge=1, le=10,
        description="for kind=simplify: most simplification tasks one run enqueues (default 3)")

    @field_validator("max_tasks")
    @classmethod
    def _only_for_simplify(cls, v, info):
        if v is not None and info.data.get("kind") not in (None, "simplify"):
            raise ValueError("max_tasks only applies to kind simplify")
        return v

    @model_validator(mode="after")
    def _default_max_tasks(self):
        if self.kind == "simplify" and self.max_tasks is None:
            self.max_tasks = 3
        return self


class Check(_Strict):
    name: str
    run: str = Field(description="shell command run against the live environment (AUTONOME_APP_URL is set)")
    timeout_s: int = 900


class SmokeTest(_Strict):
    """After the last task of a plan merges, the feature is tried in a running environment."""

    up: str = Field(description="starts the environment and waits until it is ready, e.g. docker compose up -d --wait")
    down: str = Field(description="stops it; always runs, even after a failure")
    url: str = Field(description="where the app answers, e.g. http://127.0.0.1:8000")
    seed: str | None = Field(default=None, description="loads synthetic data once the environment is up")
    checks: list[Check] = Field(default=[], description="scripted e2e/smoke tests, run before the tester")
    tester: bool = Field(default=True, description="an agent exercises the plan's acceptance criteria on the running app")
    max_fixes: int = Field(default=2, description="fix rounds before the plan is blocked for a human")


class TaskSize(_Strict):
    """How big one task may be: plans split bigger tasks and the runner records the ones that go over."""

    max_lines: int = Field(default=300, ge=20, description="changed lines of code per task, tests and docs not counted")


class Merge(_Strict):
    """How a finished task reaches main: the runner fast-forwards it, or opens a pull request."""

    mode: Literal["ff", "pr"] = "ff"
    pr: Literal["manual", "auto"] = "manual"


class Review(_Strict):
    """An independent agent reviews each task's diff against its plan and the conventions before merge."""

    enabled: bool = True
    max_rounds: int = Field(default=2, description="times the implementer is sent back with blocking findings")
    retry_medium: bool = Field(default=False, description=(
        "send the implementer back once for medium findings too; off, they become loose ends at merge"))


class Triage(_Strict):
    """A classifier judges deviation decisions instead of the human. The Jev key lives in the runner's
    environment (or ~/.autonome/secrets.env), never here."""

    model: Literal["jev-latest", "haiku", "off"] = "jev-latest"
    fallback: Literal["haiku", "off"] = "haiku"
    reject_at: float = Field(default=0.7, gt=0, le=1, description="share of noul answers at which a deviation is rejected")
    max_rounds: int = Field(default=2, ge=1, description="reviewer rounds before a rejected deviation stays proposed")

    @field_validator("model", "fallback", mode="before")
    @classmethod
    def _yaml_off(cls, v):
        return "off" if v is False else v  # YAML 1.1 reads a bare `off` as false


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class RoleModel(_Strict):
    model: str
    effort: Effort
    fallback: str | None = Field(
        default=None, description="model to switch to when this one is overloaded or unavailable (not the usage limit)")


# Who does what, on which model and with how much thinking. The preflight is measured with
# `autonome bench` (docs/benchmarks/models.md); the reviewer is on Sonnet by the human's choice; the other roles are still a guess.
DEFAULT_MODELS: dict[str, RoleModel] = {
    "implementer": RoleModel(model="sonnet", effort="medium"),
    "reviewer": RoleModel(model="sonnet", effort="medium"),
    "preflight": RoleModel(model="haiku", effort="low"),
    "tester": RoleModel(model="sonnet", effort="medium"),
    "retro": RoleModel(model="sonnet", effort="medium"),
    "handoff": RoleModel(model="sonnet", effort="low"),
    "docs": RoleModel(model="sonnet", effort="low"),
    "simplifier": RoleModel(model="haiku", effort="low"),
    "staff": RoleModel(model="opus", effort="high", fallback=None),
}


# Label and one line for each config field that has no `description` in the schema, by config path
# (list and dict levels leave no segment: `gates.name`, `models.effort`). The settings page shows them.
FIELD_HELP: dict[str, tuple[str, str]] = {
    "gates": ("Gates", "Run from cheapest to most expensive; the first one that blocks and fails stops the rest."),
    "gates.name": ("Name", "Identifies the gate in results and failure messages."),
    "gates.mode": ("Mode", "fix runs and moves on; block must pass for the task to continue."),
    "gates.timeout_s": ("Timeout (s)", "Seconds until the gate is interrupted and counted as a failure."),
    "retries": ("Retries", "How many times the implementer goes back to a task whose gates failed."),
    "retries.max": ("Max retries", "Attempts after the first failure; beyond that the task is blocked."),
    "retries.stop_on_repeat": ("Stop on repeat", "Blocks the task when the same failure shows up twice in a row."),
    "budget": ("Budget", "Token and time limits per task and per plan."),
    "budget.task_tokens": ("Task tokens", "Token ceiling for one task. Accepts 400k or 3M."),
    "budget.task_minutes": ("Task minutes", "Minutes ceiling for one task."),
    "budget.plan_tokens": ("Plan tokens", "Token ceiling for all the tasks of a plan. Accepts 400k or 3M."),
    "hold_before_merge": ("Hold before merge", "Stops before merge until you approve. Blocks the queue."),
    "hold_before_merge.features": ("Hold before merge", "Stops before merge until you approve. Blocks the queue."),
    "review_after_merge": ("Review after merge", "Merges, then you review later. Never blocks."),
    "review_after_merge.features": ("Review after merge", "Merges, then you review later. Never blocks."),
    "schedules": ("Schedules", "Scheduled checks that only queue tasks into a maintenance plan."),
    "schedules.name": ("Name", "Identifies the scheduled check."),
    "schedules.kind": ("Kind", "command runs a command; doc-drift compares docs and code; main-red watches main; "
                               "simplify reads recently changed code and proposes simplifications."),
    "schedules.every_hours": ("Every (hours)", "Interval between two runs."),
    "schedules.max_tasks": ("Max tasks", "Kind simplify only: most tasks one run queues, from 1 to 10. Empty: 3."),
    "task_size": ("Task size", "How big one task may be; plans split bigger tasks and the runner records the ones that go over."),
    "task_size.max_lines": ("Max lines", "Changed lines of code per task, tests and docs not counted. "
                                         "Plans split bigger tasks; the runner records the ones that go over."),
    "merge": ("Merge", "How a finished task reaches main."),
    "merge.mode": ("Mode", "ff: the runner fast-forwards main. pr: it pushes the task's branch and opens a pull request."),
    "merge.pr": ("Pull request", "manual: you merge it on GitHub. auto: GitHub merges it when the checks pass."),
    "review": ("Review", "A reviewer reads each task's diff before merge."),
    "review.enabled": ("Review diffs", "When off, tasks go to merge without the reviewer."),
    "smoke_test": ("Smoke test", "Tries the finished feature in a running environment, with fakes and never real services."),
    "smoke_test.checks": ("Checks", "Scripts that run against the live environment before the tester."),
    "smoke_test.checks.name": ("Name", "Identifies the check in results."),
    "smoke_test.checks.timeout_s": ("Timeout (s)", "Seconds until the check is interrupted and counted as a failure."),
    "triage": ("Triage", "A classifier judges deviations in place of the human; the Jev key stays in the runner's environment."),
    "triage.model": ("Model", "jev-latest, haiku or off; off does not write the block."),
    "triage.fallback": ("Fallback", "Model used when the main one fails: haiku or off."),
    "permissions": ("Permissions", "auto uses the Agent SDK classifier; the hard limits stay."),
    "models": ("Models", "Model and effort of each role; roles you do not change keep the default."),
    "models.model": ("Model", "The role's model name, for example sonnet or haiku."),
    "models.effort": ("Effort", "How much the model thinks: low, medium, high, xhigh or max."),
}

# One line per agent role, for the models section.
ROLE_GLOSS: dict[str, str] = {
    "implementer": "Writes the code of each task.",
    "reviewer": "Reads the task's diff before merge and sends back what blocks.",
    "preflight": "Checks the plan against main before the first task.",
    "tester": "Exercises the plan's criteria in the running app, in the smoke test.",
    "retro": "Reads a wave's evidence and proposes conventions.",
    "handoff": "Writes a session's handoff from the facts.",
    "docs": "Rewrites the features' living docs.",
    "simplifier": "Reads recently changed code and proposes simplification tasks.",
    "staff": "Senior agent that takes over a task about to block: fixes it, closes it as already satisfied, "
             "or revises its spec.",
}


class GateConfig(_Strict):
    """Gates run cheapest first; the first blocking failure stops the rest."""

    gates: list[Gate] = []
    interactive: bool = Field(default=False, description="also run the gate hook in sessions outside the runner")
    allowed_commands: list[str] = Field(
        default=[], description="extra commands (first word) a runner session may run, besides the gates' own")
    retries: Retries = Retries()
    budget: Budget = Budget()
    task_size: TaskSize = TaskSize()
    merge: Merge = Merge()
    hold_before_merge: HoldBeforeMerge = HoldBeforeMerge()
    review_after_merge: ReviewAfterMerge = ReviewAfterMerge()
    schedules: list[Schedule] = []
    review: Review = Review()
    retro_after_wave: bool = Field(default=True, description="propose conventions from each finished wave's evidence")
    language: str = Field(
        default="", description="language of what agents write for people (plans, handoffs, findings, docs), e.g. "
                                "pt-BR; empty: English. Code, identifiers and commits follow the project")
    permissions: Literal["allowlist", "auto"] = Field(
        default="allowlist",
        description="how implementer sessions are allowed to act: allowlist (only listed commands) or auto "
                    "(the Agent SDK's classifier judges each call; hard limits stay)")
    smoke_test: SmokeTest | None = None
    triage: Triage | None = None
    models: dict[str, RoleModel] = Field(
        default_factory=lambda: dict(DEFAULT_MODELS),
        description="model and effort per role; roles left out keep their default")

    @field_validator("models", mode="before")
    @classmethod
    def _over_defaults(cls, v):
        if unknown := sorted(set(v or {}) - set(DEFAULT_MODELS)):
            raise ValueError(f"unknown model roles: {', '.join(unknown)}")
        return {**{k: r.model_dump() for k, r in DEFAULT_MODELS.items()}, **(v or {})}

    @classmethod
    def from_yaml(cls, text: str) -> "GateConfig":
        return cls.model_validate(yaml.safe_load(text) or {})

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
