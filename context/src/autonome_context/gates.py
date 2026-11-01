"""Gate configuration: the orchestration of a project's gates. The rules themselves (linters,
tests, Semgrep) live in the project's repo; this only says what runs, in which order, and how."""

from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class Gate(BaseModel):
    name: str
    run: str = Field(description="shell command, run from the repo root")
    mode: Literal["fix", "block"] = "block"
    fast: bool = Field(default=False, description="part of the inner loop the implementer runs while working")
    when: list[str] = Field(default=[], description="path globs; the gate runs only if a changed file matches")
    timeout_s: int = 900


class Encore(BaseModel):
    max: int = 3
    stop_on_repeat: bool = True


class Budget(BaseModel):
    task_tokens: int = 400_000
    task_minutes: int = 30
    plan_tokens: int = 3_000_000


class Fermata(BaseModel):
    features: list[str] = []


class Playback(BaseModel):
    """Features reviewed after merge: their tasks merge as usual and wait for the human's review in the
    background ("review forward"); a fix becomes a new task, never a block."""

    features: list[str] = []


class Cue(BaseModel):
    name: str
    kind: Literal["command", "doc-drift", "main-red"]
    every_hours: int = 24
    run: str | None = Field(default=None, description="for kind=command: non-empty output or failure enqueues a task")
    task: str | None = Field(default=None, description="title of the task to enqueue")


class Check(BaseModel):
    name: str
    run: str = Field(description="shell command run against the live environment (AUTONOME_APP_URL is set)")
    timeout_s: int = 900


class Soundcheck(BaseModel):
    """After the last task of a plan merges, the feature is tried in a running environment."""

    up: str = Field(description="starts the environment and waits until it is ready, e.g. docker compose up -d --wait")
    down: str = Field(description="stops it; always runs, even after a failure")
    url: str = Field(description="where the app answers, e.g. http://127.0.0.1:8000")
    seed: str | None = Field(default=None, description="loads synthetic data once the environment is up")
    checks: list[Check] = Field(default=[], description="scripted e2e/smoke tests, run before the listener")
    listener: bool = Field(default=True, description="an agent exercises the plan's acceptance criteria on the running app")
    max_fixes: int = Field(default=2, description="fix rounds before the plan is blocked for a human")


class Review(BaseModel):
    """An independent agent reviews each task's diff against its plan and the conventions before merge."""

    enabled: bool = True
    max_rounds: int = Field(default=2, description="times the implementer is sent back with blocking findings")


class Triage(BaseModel):
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


class RoleModel(BaseModel):
    model: str
    effort: Effort
    fallback: str | None = Field(
        default=None, description="model to switch to when this one is overloaded or unavailable (not the usage limit)")


# Who does what, on which model and with how much thinking. Rehearsal is measured with
# `autonome bench` (docs/benchmarks/models.md); the reviewer stays on Sonnet; the other roles are still a guess.
DEFAULT_MODELS: dict[str, RoleModel] = {
    "musician": RoleModel(model="sonnet", effort="medium"),
    "reviewer": RoleModel(model="sonnet", effort="medium"),
    "rehearsal": RoleModel(model="haiku", effort="low"),
    "listener": RoleModel(model="sonnet", effort="medium"),
    "tune": RoleModel(model="sonnet", effort="medium"),
    "handoff": RoleModel(model="sonnet", effort="low"),
    "docs": RoleModel(model="sonnet", effort="low"),
}


class GateConfig(BaseModel):
    """Gates run cheapest first; the first blocking failure stops the rest."""

    gates: list[Gate] = []
    interactive: bool = Field(default=False, description="also run the metronome in sessions outside conduct")
    allowed_commands: list[str] = Field(
        default=[], description="extra commands (first word) a conduct session may run, besides the gates' own")
    encore: Encore = Encore()
    budget: Budget = Budget()
    fermata: Fermata = Fermata()
    playback: Playback = Playback()
    cues: list[Cue] = []
    review: Review = Review()
    tune_after_wave: bool = Field(default=True, description="propose conventions from each finished wave's evidence")
    language: str = Field(
        default="", description="language of what agents write for people (plans, handoffs, findings, docs), e.g. "
                                "pt-BR; empty: English. Code, identifiers and commits follow the project")
    soundcheck: Soundcheck | None = None
    triage: Triage | None = None
    models: dict[str, RoleModel] = Field(
        default_factory=lambda: dict(DEFAULT_MODELS),
        description="model and effort per role; roles left out keep their default")

    @field_validator("models", mode="before")
    @classmethod
    def _over_defaults(cls, v):
        return {**{k: r.model_dump() for k, r in DEFAULT_MODELS.items()}, **(v or {})}

    @classmethod
    def from_yaml(cls, text: str) -> "GateConfig":
        return cls.model_validate(yaml.safe_load(text) or {})

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)


EXAMPLE = """\
# Gates run from the repo root, cheapest first. mode fix: run and carry on; block: must pass.
gates:
  - {name: format, run: ./mvnw -q spotless:apply, mode: fix, fast: true}
  - {name: lint, run: ./mvnw -q checkstyle:check pmd:check, fast: true}
  - {name: test, run: ./mvnw -q verify}
  - {name: secrets, run: gitleaks detect --no-banner}
interactive: false
allowed_commands: [./mvnw]
encore: {max: 3, stop_on_repeat: true}
budget: {task_tokens: 400000, task_minutes: 30, plan_tokens: 3000000}
fermata: {features: []}     # stop before merge for the human (blocks the setlist)
playback: {features: []}    # merge, then the human reviews forward (never blocks)
cues: []
review: {enabled: true, max_rounds: 2}
# language: pt-BR           # what agents write for people; code and commits follow the project
# models: {musician: {model: sonnet, effort: medium, fallback: haiku}}   # per role; see DEFAULT_MODELS
# triage:                    # a classifier judges deviation decisions (the Jev key stays in the runner's environment)
#   model: jev-latest        # jev-latest | haiku | off
#   fallback: haiku          # haiku | off
#   reject_at: 0.7
#   max_rounds: 2
# soundcheck:                # try each finished feature in a running environment (fakes, never real services)
#   up: docker compose -f infra/compose.yml up -d --wait
#   down: docker compose -f infra/compose.yml down
#   url: http://127.0.0.1:8081
#   checks: [{name: e2e, run: ./scripts/e2e.sh}]
#   listener: true
#   max_fixes: 2
"""
