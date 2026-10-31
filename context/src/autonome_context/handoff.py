from typing import Literal

from pydantic import BaseModel, Field


class PlanItem(BaseModel):
    item: str
    status: Literal["done", "partial", "skipped", "changed"]
    note: str = ""


class GateResult(BaseModel):
    name: str
    passed: bool
    encores: int = 0
    root_cause: str = ""


class Decision(BaseModel):
    title: str
    rationale: str


class Handoff(BaseModel):
    """What one unit of work left behind. Written by an agent other than the one that did the work."""

    project: str
    session_id: str
    feature: str | None = Field(default=None, description="kebab-case slug, e.g. credit-inquiry")
    layers: list[str] = []
    summary: str
    plan_adherence: list[PlanItem] = []
    gates: list[GateResult] = []
    deviations: list[str] = []
    decisions: list[Decision] = []
    friction: list[str] = []
    adherence_score: float | None = Field(default=None, ge=0, le=1)
    commit_sha: str | None = None
