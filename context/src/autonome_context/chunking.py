"""Chunking strategies. Each one is a lab variable: the same handoff can be indexed by several."""

from dataclasses import dataclass
from typing import Protocol

from .handoff import Handoff


@dataclass(frozen=True)
class Chunk:
    ord: int
    section: str
    text: str


class Strategy(Protocol):
    name: str
    params: dict

    def chunk(self, h: Handoff) -> list[Chunk]: ...


def _sections(h: Handoff) -> list[tuple[str, str]]:
    """Each handoff section rendered as plain text, empty ones dropped."""
    sections = [
        ("summary", h.summary),
        ("plan_adherence", "\n".join(f"[{p.status}] {p.item}. {p.note}".strip() for p in h.plan_adherence)),
        ("gates", "\n".join(
            f"{g.name}: {'passed' if g.passed else 'failed'}, {g.encores} encores. {g.root_cause}".strip()
            for g in h.gates
        )),
        ("deviations", "\n".join(h.deviations)),
        ("decisions", "\n".join(f"{d.title}: {d.rationale}" for d in h.decisions)),
        ("friction", "\n".join(h.friction)),
    ]
    return [(name, text) for name, text in sections if text.strip()]


class SectionV1:
    """One chunk per section, prefixed with where it came from, so the vector carries the context."""

    name = "section-v1"
    params = {"unit": "section", "context_prefix": True}

    def chunk(self, h: Handoff) -> list[Chunk]:
        header = f"feature: {h.feature or 'unknown'} | layers: {', '.join(h.layers) or 'unknown'}"
        return [
            Chunk(i, name, f"{header}\nsection: {name}\n{text}")
            for i, (name, text) in enumerate(_sections(h))
        ]


STRATEGIES: dict[str, Strategy] = {s.name: s for s in (SectionV1(),)}
DEFAULT_STRATEGY = SectionV1.name
