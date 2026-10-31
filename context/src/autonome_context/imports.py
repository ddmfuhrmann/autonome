"""Bring a project's existing knowledge into its memory: ADRs become decisions, a roadmap becomes the
backlog compose draws waves from. Both are idempotent: running them again updates, never duplicates."""

import json
import re
from pathlib import Path

from psycopg import AsyncConnection

from . import store

_SECTION = re.compile(r"^##\s+(.+?)\s*$", re.M)


def parse_adr(text: str, name: str) -> dict:
    """An ADR file (`# NNNN — Title`, `**Status:** …`, `## Context/Decision/Consequences`) as a decision."""
    head = re.search(r"^#\s+(\d+)\s*[—–-]\s*(.+?)\s*$", text, re.M)
    number, title = (int(head.group(1)), head.group(2)) if head else (None, Path(name).stem)
    field = lambda key: (m.group(1).strip() if (m := re.search(rf"\*\*{key}:\*\*\s*(.+)", text)) else "")
    status_text = field("Status")
    parts = _SECTION.split(text)
    sections = {parts[i].strip().lower(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    status = "superseded" if status_text.lower().startswith("superseded") else \
        "proposed" if status_text.lower().startswith("proposed") else "accepted"
    return {"adr": number, "title": title, "status": status, "status_text": status_text, "date": field("Date"),
            "context": sections.get("context", ""), "decision": sections.get("decision", ""),
            "consequences": sections.get("consequences", ""), "source": name}


async def import_adrs(conn: AsyncConnection, indexer: store.Indexer, project: str, folder: Path) -> list[str]:
    existing = {d["body"].get("adr"): d for d in await store.list_documents(conn, project, type="decision", limit=1000)
                if d["body"].get("adr") is not None}
    done = []
    for path in sorted(folder.glob("*.md")):
        adr = parse_adr(path.read_text(), path.name)
        status = adr.pop("status")
        async with conn.transaction():
            if adr["adr"] in existing:
                doc = existing[adr["adr"]]
                await store.add_version(conn, indexer, doc["id"], adr)
                await store.set_status(conn, doc["id"], status)
            else:
                await store.create_document(conn, indexer, project=project, type="decision", status=status, body=adr)
        done.append(f"ADR {adr['adr']:04d} {status}: {adr['title']}" if adr["adr"] else f"{path.name} {status}")
    return done


def parse_roadmap(html: str) -> list[dict]:
    """The JSON block of a kanban roadmap ({waves: [{n, name}], cards: [{id, wave, title, status, …}]})
    as one backlog per wave."""
    m = re.search(r"<script[^>]*(?:id=\"roadmap-data\"|type=\"application/json\")[^>]*>(.*?)</script>", html, re.S)
    if not m:
        raise ValueError("no JSON roadmap block found")
    data = json.loads(m.group(1))
    waves = []
    for w in data["waves"]:
        items = [{k: v for k, v in c.items() if k != "wave"} for c in data["cards"] if c["wave"] == w["n"]]
        waves.append({"wave": w["n"], "title": f"Wave {w['n']}: {w['name']}", "items": items})
    return waves


async def import_backlog(conn: AsyncConnection, indexer: store.Indexer, project: str, waves: list[dict]) -> list[str]:
    existing = {d["body"].get("wave"): d for d in await store.list_documents(conn, project, type="backlog", limit=100)}
    done = []
    for w in waves:
        status = "done" if w["items"] and all(i.get("status") == "done" for i in w["items"]) else "open"
        async with conn.transaction():
            if w["wave"] in existing:
                await store.add_version(conn, indexer, existing[w["wave"]]["id"], w)
                await store.set_status(conn, existing[w["wave"]]["id"], status)
            else:
                await store.create_document(conn, indexer, project=project, type="backlog", status=status, body=w)
        done.append(f"{w['title']}: {len(w['items'])} items, {status}")
    return done
