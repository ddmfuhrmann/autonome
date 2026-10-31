"""Gate configuration: the orchestration of a project's gates. The rules themselves (linters,
tests, Semgrep) live in the project's repo; this only says what runs, in which order, and how."""

from typing import Literal

import yaml
from pydantic import BaseModel, Field


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


class Cue(BaseModel):
    name: str
    kind: Literal["command", "doc-drift", "main-red"]
    every_hours: int = 24
    run: str | None = Field(default=None, description="for kind=command: non-empty output or failure enqueues a task")
    task: str | None = Field(default=None, description="title of the task to enqueue")


class GateConfig(BaseModel):
    """Gates run cheapest first; the first blocking failure stops the rest."""

    gates: list[Gate] = []
    interactive: bool = Field(default=False, description="also run the metronome in sessions outside conduct")
    allowed_commands: list[str] = Field(
        default=[], description="extra commands (first word) a conduct session may run, besides the gates' own")
    encore: Encore = Encore()
    budget: Budget = Budget()
    fermata: Fermata = Fermata()
    cues: list[Cue] = []

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
fermata: {features: []}
cues: []
"""
