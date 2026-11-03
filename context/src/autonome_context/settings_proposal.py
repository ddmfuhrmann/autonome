"""The settings the init proposes: kept on the project, never read as the gate config."""

import json
from datetime import datetime, timezone

import yaml
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from . import settings_form, store
from .gates import GateConfig

NOTE_MAX = 500
YAML_MAX = 64 * 1024


def _slug_error(project: str) -> str | None:
    if not project or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in project):
        return "The project slug is empty or has a space or a control character."
    return None


def _problems(text: str, note: str, catalog: set[str]) -> dict[str, str]:
    if len(note) > NOTE_MAX:
        return {"note": f"The note has {len(note)} characters; the maximum is {NOTE_MAX}."}
    if settings_form._bad_name(note):
        return {"note": "The note has a control character."}
    if len(text.encode()) > YAML_MAX:
        return {"yaml": f"The YAML is larger than {YAML_MAX // 1024} KB."}
    try:
        cfg = GateConfig.from_yaml(text)
    except yaml.YAMLError as e:
        return {"yaml": f"Invalid YAML: {getattr(e, 'problem', None) or e}"}
    except Exception as e:  # schema error
        return {"yaml": str(e)}
    return settings_form.extra_checks(cfg, catalog)


async def propose(conn: AsyncConnection, project: str, yaml_text: str, note: str = "") -> dict:
    """Validates and stores the proposal, replacing the previous one. {ok} or {error, problems}."""
    if error := _slug_error(project):
        return {"error": error, "problems": {"project": error}}
    catalog = {f["slug"] for f in await store.catalog(conn, project)}
    if problems := _problems(yaml_text, note, catalog):
        return {"error": "The proposal was not saved.", "problems": problems}
    await store.ensure_project(conn, project)
    proposal = {"yaml": yaml_text, "note": note, "at": datetime.now(timezone.utc).isoformat()}
    await conn.execute("update project set settings_proposal = %s where slug = %s",
                       (Jsonb(proposal), project))
    await conn.commit()
    return {"ok": True}


async def get(conn: AsyncConnection, project: str) -> dict | None:
    row = await (await conn.execute("select settings_proposal from project where slug = %s",
                                    (project,))).fetchone()
    value = row[0] if row else None
    return json.loads(value) if isinstance(value, str) else value


async def clear(conn: AsyncConnection, project: str) -> None:
    await conn.execute("update project set settings_proposal = null where slug = %s", (project,))
    await conn.commit()
