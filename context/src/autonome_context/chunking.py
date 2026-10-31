"""Chunking strategies. Each one is a lab variable: every document is indexed by all of them,
so they can be compared side by side on the same data."""

from dataclasses import dataclass
from typing import Protocol

from .handoff import Handoff

# Fields that describe a document rather than say something about it.
_META_KEYS = {"project", "session_id", "commit_sha", "feature", "layers", "status", "adherence_score"}
_WHOLE_MAX_CHARS = 8000


@dataclass(frozen=True)
class Doc:
    """What a strategy sees: the latest body of a document plus where it lives."""

    type: str
    body: dict
    feature: str | None
    layers: list[str]


@dataclass(frozen=True)
class Chunk:
    ord: int
    section: str
    text: str


class Strategy(Protocol):
    name: str
    params: dict

    def chunk(self, doc: Doc) -> list[Chunk]: ...


def _handoff_sections(h: Handoff) -> list[tuple[str, str]]:
    return [
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


def _render(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_render(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_render(v)}" for k, v in value.items() if v not in (None, "", []))
    return ""


def sections(doc: Doc) -> list[tuple[str, str]]:
    """The document as (section name, text) pairs, empty ones dropped."""
    if doc.type == "handoff":
        pairs = _handoff_sections(Handoff.model_validate(doc.body))
    elif doc.type == "backlog":
        # One section per item, so a search lands on the item, not on the whole wave.
        pairs = [(item.get("id", "item"), f"{doc.body.get('title', '')} | {item.get('title', '')} [{item.get('status', '')}]"
                  + (f" {item['note']}" if item.get("note") else "") + (" (PRD)" if item.get("prd") else ""))
                 for item in doc.body.get("items", [])]
    else:
        pairs = [(k, _render(v)) for k, v in doc.body.items() if k not in _META_KEYS]
    return [(name, text) for name, text in pairs if text.strip()]


def _header(doc: Doc) -> str:
    return f"{doc.type} | feature: {doc.feature or 'none'} | layers: {', '.join(doc.layers) or 'none'}"


class WholeV1:
    """Baseline: one vector for the whole document."""

    name = "whole-v1"
    params = {"unit": "document", "context_prefix": True, "max_chars": _WHOLE_MAX_CHARS}

    def chunk(self, doc: Doc) -> list[Chunk]:
        parts = sections(doc)
        if not parts:
            return []
        body = "\n".join(f"{name}: {text}" for name, text in parts)
        return [Chunk(0, "whole", f"{_header(doc)}\n{body}"[:_WHOLE_MAX_CHARS])]


class SectionV1:
    """One chunk per section, prefixed with where it came from, so the vector carries the context."""

    name = "section-v1"
    params = {"unit": "section", "context_prefix": True}

    def chunk(self, doc: Doc) -> list[Chunk]:
        header = _header(doc)
        return [Chunk(i, name, f"{header}\nsection: {name}\n{text}") for i, (name, text) in enumerate(sections(doc))]


class SectionBareV1:
    """One chunk per section, text only. Against section-v1 it measures what the context prefix buys."""

    name = "section-bare-v1"
    params = {"unit": "section", "context_prefix": False}

    def chunk(self, doc: Doc) -> list[Chunk]:
        return [Chunk(i, name, text) for i, (name, text) in enumerate(sections(doc))]


STRATEGIES: dict[str, Strategy] = {s.name: s for s in (SectionV1(), WholeV1(), SectionBareV1())}
DEFAULT_STRATEGY = SectionV1.name
