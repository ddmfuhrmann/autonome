"""The backlog: one document per roadmap wave holding its items. Items are edited, added and moved
between waves here; every change is a new version of the wave documents it touches, and appends
`{at, by, item, field, old, new}` to their `changes`, so an item's history survives a move."""

import difflib
from datetime import UTC, datetime

from psycopg import AsyncConnection

from . import rules, store

STATUSES = ("backlog", "next", "doing", "done")
_FIELDS = ("title", "status", "note", "prd")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


async def waves(conn: AsyncConnection, project: str) -> list[dict]:
    docs = await store.list_documents(conn, project, type="backlog", limit=200)
    return sorted(docs, key=lambda d: d["body"].get("wave", 0))


async def list_backlog(conn: AsyncConnection, project: str, wave: int | None = None) -> list[dict]:
    return [{"wave": d["body"]["wave"], "title": d["body"]["title"], "status": d["status"],
             "items": d["body"]["items"]}
            for d in await waves(conn, project) if wave is None or d["body"]["wave"] == wave]


def _status_of(items: list[dict]) -> str:
    return "done" if items and all(i.get("status") == "done" for i in items) else "open"


async def _save(conn: AsyncConnection, indexer: store.Indexer | None, doc: dict, items: list[dict],
                changes: list[dict]) -> None:
    body = {**doc["body"], "items": items, "changes": doc["body"].get("changes", []) + changes}
    await store.add_version(conn, indexer, doc["id"], body)
    await store.set_status(conn, doc["id"], _status_of(items))


async def _locate(conn: AsyncConnection, project: str, key: str) -> tuple[dict, int]:
    all_waves = await waves(conn, project)
    for doc in all_waves:
        for i, item in enumerate(doc["body"]["items"]):
            if item["id"] == key:
                return doc, i
    keys = [item["id"] for doc in all_waves for item in doc["body"]["items"]]
    close = difflib.get_close_matches(key, keys, n=3)
    raise rules.Forbidden(f"no backlog item {key!r} in {project}" + (f"; did you mean {', '.join(close)}?" if close else ""))


async def _wave_doc(conn: AsyncConnection, project: str, wave: int) -> dict:
    all_waves = await waves(conn, project)
    for doc in all_waves:
        if doc["body"]["wave"] == wave:
            return doc
    raise rules.Forbidden(f"no wave {wave} in the backlog of {project}; waves: "
                          + ", ".join(str(d["body"]["wave"]) for d in all_waves))


def _check_status(status: str | None) -> None:
    if status is not None and status not in STATUSES:
        raise rules.Forbidden(f"status must be one of {', '.join(STATUSES)}, not {status!r}")


async def update_item(conn: AsyncConnection, indexer: store.Indexer | None, project: str, key: str, *, by: str,
                      wave: int | None = None, **fields) -> dict:
    """Changes an item's fields and, with `wave`, moves it to another wave. Fields left None stay."""
    _check_status(fields.get("status"))
    async with conn.transaction():
        doc, index = await _locate(conn, project, key)
        item = doc["body"]["items"][index]
        at = _now()
        new = dict(item)
        changes = []
        for field in _FIELDS:
            value = fields.get(field)
            if value is None or (value or None) == (item.get(field) or None):
                continue  # not given, or the same (an empty note equals no note)
            changes.append({"at": at, "by": by, "item": key, "field": field, "old": item.get(field), "new": value or None})
            if value == "":
                new.pop(field, None)
            else:
                new[field] = value
        if wave is not None and wave != doc["body"]["wave"]:
            target = await _wave_doc(conn, project, wave)
            move = {"at": at, "by": by, "item": key, "field": "wave", "old": doc["body"]["wave"], "new": wave}
            await _save(conn, indexer, doc, [i for i in doc["body"]["items"] if i["id"] != key], [move])
            await _save(conn, indexer, target, target["body"]["items"] + [new], changes + [move])
            return {"item": new, "wave": wave, "changes": changes + [move]}
        if changes:
            items = list(doc["body"]["items"])
            items[index] = new
            await _save(conn, indexer, doc, items, changes)
        return {"item": new, "wave": doc["body"]["wave"], "changes": changes}


async def add_item(conn: AsyncConnection, indexer: store.Indexer | None, project: str, wave: int, key: str,
                   title: str, *, by: str, status: str = "backlog", note: str | None = None,
                   prd: bool | str | None = None) -> dict:
    _check_status(status)
    if not key.strip() or not title.strip():
        raise rules.Forbidden("an item needs an id and a title")
    async with conn.transaction():
        try:
            doc, _ = await _locate(conn, project, key)
        except rules.Forbidden:
            pass
        else:
            raise rules.Forbidden(f"backlog item {key!r} already exists, in wave {doc['body']['wave']}")
        target = await _wave_doc(conn, project, wave)
        item = {"id": key, "title": title, "status": status}
        if note:
            item["note"] = note
        if prd is not None:
            item["prd"] = prd
        await _save(conn, indexer, target, target["body"]["items"] + [item],
                    [{"at": _now(), "by": by, "item": key, "field": "added", "old": None, "new": title}])
    return {"item": item, "wave": wave}


async def remove_item(conn: AsyncConnection, indexer: store.Indexer | None, project: str, key: str, *, by: str,
                      reason: str = "") -> dict:
    """Takes an item out of the backlog. Earlier versions keep it, and the change records what it was."""
    async with conn.transaction():
        doc, index = await _locate(conn, project, key)
        item = doc["body"]["items"][index]
        await _save(conn, indexer, doc, [i for i in doc["body"]["items"] if i["id"] != key],
                    [{"at": _now(), "by": by, "item": key, "field": "removed", "old": item, "new": None,
                      **({"reason": reason} if reason else {})}])
    return {"removed": item, "wave": doc["body"]["wave"]}


async def item_history(conn: AsyncConnection, project: str, key: str) -> list[dict]:
    return sorted((c for d in await waves(conn, project) for c in d["body"].get("changes", []) if c["item"] == key),
                  key=lambda c: c["at"])
