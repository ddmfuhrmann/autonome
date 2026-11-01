from typing import Literal

from pydantic import BaseModel, Field


class PlanItem(BaseModel):
    item: str
    status: Literal["done", "partial", "skipped", "changed"]
    note: str = ""


class GateResult(BaseModel):
    name: str
    passed: bool
    retries: int = 0
    root_cause: str = ""


class Decision(BaseModel):
    """A choice the plan and the task did not already make: an alternative weighed, an ambiguity
    resolved. Restating the spec is not a decision."""

    title: str
    rationale: str
    deviation: bool = Field(
        default=False, description="true when the choice departs from the plan or a convention; only these "
                                   "wait for the human's review")


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
