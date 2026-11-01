"""Who may move a document from one status to another. Human-only transitions are the human
gates of the process; the context enforces them, so no agent (or process) can skip them."""

from typing import Literal

from psycopg import AsyncConnection

from . import store

Actor = Literal["human", "agent", "runner"]

# (type, from, to) -> actors allowed
TRANSITIONS: dict[tuple[str, str, str], set[str]] = {
    ("plan", "draft", "approved"): {"human"},
    ("plan", "draft", "abandoned"): {"human", "agent"},
    ("plan", "approved", "abandoned"): {"human"},
    ("plan", "approved", "done"): {"runner"},
    ("plan", "approved", "held"): {"runner"},  # the rehearsal found the plan out of date
    ("plan", "held", "approved"): {"human", "runner"},  # accept the revision or keep the original; the runner only with a triage verdict
    ("plan", "held", "abandoned"): {"human"},
    ("plan", "approved", "blocked"): {"runner"},  # the soundcheck kept failing after its fix rounds
    ("plan", "blocked", "approved"): {"human"},  # try again: one more soundcheck, fresh fix rounds
    ("plan", "blocked", "done"): {"human"},  # accept the feature as it is
    ("plan", "blocked", "abandoned"): {"human"},
    ("wave", "draft", "approved"): {"human"},
    ("wave", "draft", "abandoned"): {"human", "agent"},
    ("wave", "approved", "abandoned"): {"human"},
    ("wave", "approved", "done"): {"runner"},
    ("task", "pending", "playing"): {"runner"},
    ("task", "playing", "done"): {"runner"},
    ("task", "playing", "blocked"): {"runner"},
    ("task", "playing", "on_hold"): {"runner"},
    ("task", "playing", "pending"): {"runner"},  # interrupted run, try again later
    ("task", "on_hold", "released"): {"human"},
    ("task", "on_hold", "pending"): {"human"},  # rejected at the fermata: play it again
    ("task", "released", "done"): {"runner"},
    ("task", "released", "pending"): {"runner"},  # could not merge (conflict, gates on top of main)
    ("task", "blocked", "pending"): {"human"},
    ("task", "blocked", "abandoned"): {"human"},
    ("task", "pending", "abandoned"): {"human"},
    ("convention", "proposed", "active"): {"human"},  # suggested by the tune loop
    ("convention", "proposed", "rejected"): {"human"},
    ("decision", "proposed", "accepted"): {"human", "runner"},  # the runner only with a triage verdict
    ("decision", "proposed", "rejected"): {"human", "runner"},
    ("decision", "accepted", "proposed"): {"runner"},  # the screening found a deviation the scribe missed
    ("decision", "accepted", "superseded"): {"human"},
}

# Documents whose content only a human may change (from the UI).
HUMAN_EDITED_TYPES = {"convention", "gate_config"}


class Forbidden(Exception):
    pass


def allowed(type: str, current: str | None, to: str, actor: Actor) -> bool:
    return actor in TRANSITIONS.get((type, current or "", to), set())


async def transition(conn: AsyncConnection, doc_id: int, to: str, actor: Actor) -> dict:
    doc = await store.get_document(conn, doc_id)
    if doc is None:
        raise Forbidden(f"document {doc_id} does not exist")
    if not allowed(doc["type"], doc["status"], to, actor):
        raise Forbidden(f"{actor} may not move {doc['type']} #{doc_id} from {doc['status']} to {to}")
    await store.set_status(conn, doc_id, to)
    return {**doc, "status": to}
