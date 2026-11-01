import fnmatch
import os
import gzip
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .chunking import Chunk, Doc, Strategy
from .embed import Embedder
from .gates import GateConfig
from .handoff import Handoff
from .redact import redact

log = logging.getLogger(__name__)

SearchMode = Literal["hybrid", "vector", "fts"]


@dataclass
class Indexer:
    strategies: list[Strategy]
    embedder: Embedder


def _redact_strings(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_strings(v) for k, v in value.items()}
    return value


async def _id(conn: AsyncConnection, sql: str, params: tuple) -> int:
    row = await (await conn.execute(sql, params)).fetchone()
    return row[0]


async def ensure_project(conn: AsyncConnection, slug: str) -> int:
    return await _id(
        conn,
        "insert into project (slug, name) values (%s, %s)"
        " on conflict (slug) do update set slug = excluded.slug returning id",
        (slug, slug),
    )


async def _ensure_feature(conn: AsyncConnection, project_id: int, slug: str) -> int:
    return await _id(
        conn,
        "insert into feature (project_id, slug, name) values (%s, %s, %s)"
        " on conflict (project_id, slug) do update set slug = excluded.slug returning id",
        (project_id, slug, slug),
    )


async def _ensure_layer(conn: AsyncConnection, project_id: int, slug: str) -> int:
    return await _id(
        conn,
        "insert into layer (project_id, slug) values (%s, %s)"
        " on conflict (project_id, slug) do update set slug = excluded.slug returning id",
        (project_id, slug),
    )


async def ensure_strategy(conn: AsyncConnection, strategy: Strategy, embedder: Embedder) -> int:
    """A strategy is identified by name; changing its params or model means a new name."""
    row = await (
        await conn.execute("select id, embed_model, dims from strategy where name = %s", (strategy.name,))
    ).fetchone()
    if row:
        if (row[1], row[2]) != (embedder.model, embedder.dims):
            raise ValueError(
                f"strategy {strategy.name} was indexed with {row[1]}/{row[2]}, "
                f"not {embedder.model}/{embedder.dims}; register it under a new name"
            )
        return row[0]
    return await _id(
        conn,
        "insert into strategy (name, params, embed_model, dims) values (%s, %s, %s, %s) returning id",
        (strategy.name, json.dumps(strategy.params), embedder.model, embedder.dims),
    )


# --- documents -----------------------------------------------------------------------------------


async def create_document(
    conn: AsyncConnection,
    indexer: Indexer | None,
    *,
    project: str,
    type: str,
    body: dict,
    feature: str | None = None,
    layers: list[str] | tuple = (),
    status: str | None = None,
    commit_sha: str | None = None,
) -> int:
    """Stores a redacted document as version 1 and indexes it. Call inside a transaction."""
    body = _redact_strings(body)
    project_id = await ensure_project(conn, project)
    feature_id = await _ensure_feature(conn, project_id, feature) if feature else None
    doc_id = await _id(
        conn,
        "insert into document (project_id, feature_id, type, status) values (%s, %s, %s, %s) returning id",
        (project_id, feature_id, type, status),
    )
    for layer in dict.fromkeys(layers):
        layer_id = await _ensure_layer(conn, project_id, layer)
        await conn.execute("insert into document_layer (document_id, layer_id) values (%s, %s)", (doc_id, layer_id))
    version_id = await _id(
        conn,
        "insert into document_version (document_id, version, body, commit_sha) values (%s, 1, %s, %s) returning id",
        (doc_id, json.dumps(body), commit_sha),
    )
    if indexer:
        await _index_or_defer(conn, indexer, version_id, Doc(type, body, feature, list(layers)))
    return doc_id


async def add_version(
    conn: AsyncConnection, indexer: Indexer | None, doc_id: int, body: dict, commit_sha: str | None = None
) -> int:
    """Append-only change: a new version, indexed; older versions stay but drop out of search."""
    body = _redact_strings(body)
    version_id = await _id(
        conn,
        "insert into document_version (document_id, version, body, commit_sha)"
        " select %s, coalesce(max(version), 0) + 1, %s, %s from document_version where document_id = %s"
        " returning id",
        (doc_id, json.dumps(body), commit_sha, doc_id),
    )
    if indexer:
        doc = await get_document(conn, doc_id)
        await _index_or_defer(conn, indexer, version_id, Doc(doc["type"], body, doc["feature"], doc["layers"]))
    return version_id


async def set_status(conn: AsyncConnection, doc_id: int, status: str) -> None:
    await conn.execute("update document set status = %s where id = %s", (status, doc_id))


async def link(conn: AsyncConnection, from_id: int, to_id: int, kind: str) -> None:
    await conn.execute(
        "insert into link (from_id, to_id, kind) values (%s, %s, %s) on conflict do nothing", (from_id, to_id, kind)
    )


_DOC_SELECT = """
    select d.id, p.slug as project, d.type, d.status, f.slug as feature, d.created_at,
           dv.version, dv.body, dv.commit_sha, dv.created_at as updated_at,
           coalesce((select array_agg(l.slug order by l.slug) from document_layer dl
                     join layer l on l.id = dl.layer_id where dl.document_id = d.id), '{}') as layers
    from document d
    join project p on p.id = d.project_id
    left join feature f on f.id = d.feature_id
    join lateral (select * from document_version v where v.document_id = d.id
                  order by v.version desc limit 1) dv on true
"""


async def get_document(conn: AsyncConnection, doc_id: int) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_DOC_SELECT + " where d.id = %s", (doc_id,))
        return await cur.fetchone()


async def list_documents(
    conn: AsyncConnection,
    project: str,
    *,
    type: str | None = None,
    status: str | None = None,
    feature: str | None = None,
    limit: int | None = 200,
    oldest_first: bool = False,
) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _DOC_SELECT
            + """ where p.slug = %(project)s
                  and (%(type)s::text is null or d.type = %(type)s)
                  and (%(status)s::text is null or d.status = %(status)s)
                  and (%(feature)s::text is null or f.slug = %(feature)s)
                order by d.created_at """ + ("asc, d.id asc" if oldest_first else "desc")
            + " limit %(limit)s",
            {"project": project, "type": type, "status": status, "feature": feature, "limit": limit},
        )
        return await cur.fetchall()


@dataclass(frozen=True)
class DocFilter:
    """What the Documents page narrows by. Values inside one facet are OR'd; facets are AND'd."""

    types: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    layers: tuple[str, ...] = ()
    q: str = ""


_BROWSE_FROM = """
    from document d
    join project p on p.id = d.project_id
    left join feature f on f.id = d.feature_id
    join lateral (select v.body from document_version v where v.document_id = d.id
                  order by v.version desc limit 1) dv on true
"""

_REF = re.compile(r"^\s*(?:[A-Za-z]-|#)?(\d+)\s*$")


def _browse_where(f: DocFilter, skip: str | None = None) -> tuple[str, dict]:
    """The where clause for a filter. `skip` leaves one facet out, so its own counts stay comparable."""
    clauses, params = ["p.slug = %(project)s"], {}
    if f.types and skip != "type":
        clauses.append("d.type = any(%(types)s)")
        params["types"] = list(f.types)
    if f.statuses and skip != "status":
        clauses.append("d.status = any(%(statuses)s)")
        params["statuses"] = list(f.statuses)
    if f.features and skip != "feature":
        clauses.append("f.slug = any(%(features)s)")
        params["features"] = list(f.features)
    if f.layers and skip != "layer":
        clauses.append("exists (select 1 from document_layer dl join layer l on l.id = dl.layer_id"
                       " where dl.document_id = d.id and l.slug = any(%(layers)s))")
        params["layers"] = list(f.layers)
    if f.q.strip():
        ref = _REF.match(f.q)
        clauses.append("(dv.body->>'title' ilike %(q)s or dv.body->>'summary' ilike %(q)s"
                       + (" or d.id = %(qid)s" if ref else "") + ")")
        params["q"] = f"%{f.q.strip()}%"
        if ref:
            params["qid"] = int(ref.group(1))
    return " where " + " and ".join(clauses), params


async def browse_documents(conn: AsyncConnection, project: str, f: DocFilter, limit: int = 200) -> list[dict]:
    """The Documents list: newest first, narrowed by facets and a title/summary/ref query."""
    where, params = _browse_where(f)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"select d.id {_BROWSE_FROM} {where} order by d.created_at desc, d.id desc limit %(limit)s",
            {"project": project, "limit": limit, **params},
        )
        ids = [r["id"] for r in await cur.fetchall()]
        if not ids:
            return []
        await cur.execute(_DOC_SELECT + " where d.id = any(%(ids)s) order by d.created_at desc, d.id desc",
                          {"ids": ids})
        return await cur.fetchall()


async def document_facets(conn: AsyncConnection, project: str, f: DocFilter) -> dict[str, list[tuple[str, int]]]:
    """Counts per value for type, status, feature and layer. Each facet ignores its own selection, so
    picking `decision` still shows how many handoffs there are."""
    out: dict[str, list[tuple[str, int]]] = {}
    async with conn.cursor() as cur:
        for name, col in {"type": "d.type", "status": "d.status", "feature": "f.slug"}.items():
            where, params = _browse_where(f, skip=name)
            await cur.execute(f"select {col}, count(*) {_BROWSE_FROM} {where} and {col} is not null"
                              " group by 1 order by 2 desc, 1", {"project": project, **params})
            out[name] = [(v, n) for v, n in await cur.fetchall()]
        where, params = _browse_where(f, skip="layer")
        await cur.execute(f"""select l.slug, count(*) {_BROWSE_FROM}
                              join document_layer dl on dl.document_id = d.id join layer l on l.id = dl.layer_id
                              {where} group by 1 order by 2 desc, 1""", {"project": project, **params})
        out["layer"] = [(v, n) for v, n in await cur.fetchall()]
    return out


async def titles(conn: AsyncConnection, ids: list[int]) -> dict[int, str]:
    """A short readable title per document, for link lists."""
    if not ids:
        return {}
    async with conn.cursor() as cur:
        await cur.execute(
            """select d.id, coalesce(dv.body->>'title', left(dv.body->>'summary', 120), d.type)
               from document d join lateral (select v.body from document_version v where v.document_id = d.id
                                             order by v.version desc limit 1) dv on true
               where d.id = any(%s)""", (ids,))
        return {i: t for i, t in await cur.fetchall()}


async def links_of(conn: AsyncConnection, doc_id: int) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """select l.kind, 'out' as direction, d.id, d.type, d.status from link l join document d on d.id = l.to_id
               where l.from_id = %(id)s
               union all
               select l.kind, 'in', d.id, d.type, d.status from link l join document d on d.id = l.from_id
               where l.to_id = %(id)s""",
            {"id": doc_id},
        )
        return await cur.fetchall()


async def delete_project(conn: AsyncConnection, slug: str) -> dict[str, int]:
    """Removes a project and everything in it, children first. Irreversible: back up the database
    before (see CLAUDE.md). Returns how many rows went from each table."""
    counts: dict[str, int] = {}
    docs = "select d.id from document d join project p on p.id = d.project_id where p.slug = %(s)s"
    statements = [
        ("gate_run", "delete from gate_run where project_id = (select id from project where slug = %(s)s)"),
        ("task_event", "delete from task_event where project_id = (select id from project where slug = %(s)s)"),
        ("chunk", f"delete from chunk where document_version_id in (select id from document_version where document_id in ({docs}))"),
        ("link", f"delete from link where from_id in ({docs}) or to_id in ({docs})"),
        ("raw_transcript", f"delete from raw_transcript where document_id in ({docs})"),
        ("document_layer", f"delete from document_layer where document_id in ({docs})"),
        ("document_version", f"delete from document_version where document_id in ({docs})"),
        ("document", "delete from document where project_id = (select id from project where slug = %(s)s)"),
        ("eval_run", "delete from eval_run where project_id = (select id from project where slug = %(s)s)"),
        ("eval_question", "delete from eval_question where project_id = (select id from project where slug = %(s)s)"),
        ("feature", "delete from feature where project_id = (select id from project where slug = %(s)s)"),
        ("layer", "delete from layer where project_id = (select id from project where slug = %(s)s)"),
        ("project", "delete from project where slug = %(s)s"),
    ]
    async with conn.transaction():
        for table, sql in statements:
            counts[table] = (await conn.execute(sql, {"s": slug})).rowcount
    return counts


async def list_projects(conn: AsyncConnection) -> list[str]:
    rows = await (await conn.execute("select slug from project order by slug")).fetchall()
    return [r[0] for r in rows]


# --- handoffs ------------------------------------------------------------------------------------


async def put_handoff(conn: AsyncConnection, indexer: Indexer, h: Handoff, raw_transcript: str | None) -> int:
    """Stores a redacted handoff with its decisions and raw transcript, and indexes it. One transaction."""
    async with conn.transaction():
        doc_id = await create_document(
            conn, indexer,
            project=h.project, type="handoff", body=h.model_dump(mode="json"),
            feature=h.feature, layers=h.layers, status="final", commit_sha=h.commit_sha,
        )
        for d in h.decisions:
            decision_id = await create_document(
                conn, indexer,
                project=h.project, type="decision",
                body={"title": d.title, "decision": d.rationale, "deviation": d.deviation},
                # A choice within the plan is a fact of what was built: in effect. A deviation waits for
                # the human, whose rejection turns into a task to undo it.
                feature=h.feature, layers=h.layers, status="proposed" if d.deviation else "accepted",
            )
            await link(conn, doc_id, decision_id, "records")
        if raw_transcript:
            await conn.execute(
                "insert into raw_transcript (document_id, session_id, content_gz) values (%s, %s, %s)",
                (doc_id, h.session_id, gzip.compress(redact(raw_transcript).encode())),
            )
    return doc_id


# --- indexing ------------------------------------------------------------------------------------


async def _index_or_defer(conn: AsyncConnection, indexer: Indexer, version_id: int, doc: Doc) -> None:
    """Indexing never costs a write: if it fails, the version is kept without chunks, which is what
    `autonome-context reindex` looks for."""
    try:
        async with conn.transaction():  # a savepoint, so a failed insert does not poison the caller's
            await index_version(conn, indexer, version_id, doc)
    except Exception:
        log.exception("indexing version %s failed; it is saved and `reindex` will pick it up", version_id)


def fit(chunks: list[Chunk], max_chars: int) -> list[Chunk]:
    """Splits chunks the embedding model could not take whole, at line breaks where possible. The
    parts keep their section, so search still finds a long section through any of them."""
    out: list[Chunk] = []
    for c in chunks:
        parts, current = [], ""
        for line in c.text.splitlines(keepends=True):
            while len(line) > max_chars:  # a single line too long: hard split
                if current:
                    parts.append(current)
                    current = ""
                parts.append(line[:max_chars])
                line = line[max_chars:]
            if len(current) + len(line) > max_chars:
                parts.append(current)
                current = ""
            current += line
        parts.append(current)
        out += [Chunk(0, c.section, p) for p in parts if p.strip()]
    return [Chunk(i, c.section, c.text) for i, c in enumerate(out)]


async def index_version(conn: AsyncConnection, indexer: Indexer, version_id: int, doc: Doc) -> int:
    total = 0
    max_chars = getattr(indexer.embedder, "max_chars", 4000)
    for strategy in indexer.strategies:
        chunks = fit(strategy.chunk(doc), max_chars)
        if not chunks:
            continue
        strategy_id = await ensure_strategy(conn, strategy, indexer.embedder)
        vectors = await indexer.embedder.embed([c.text for c in chunks], "document")
        async with conn.cursor() as cur:
            await cur.executemany(
                "insert into chunk (document_version_id, strategy_id, ord, section, text, embedding)"
                " values (%s, %s, %s, %s, %s, %s::vector) on conflict do nothing",
                [(version_id, strategy_id, c.ord, c.section, c.text, str(v)) for c, v in zip(chunks, vectors)],
            )
        total += len(chunks)
    return total


async def reindex(conn: AsyncConnection, strategy: Strategy, embedder: Embedder) -> int:
    """Indexes every document version that this strategy has not seen yet."""
    strategy_id = await ensure_strategy(conn, strategy, embedder)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """select dv.id, d.type, dv.body, f.slug as feature,
                      coalesce((select array_agg(l.slug) from document_layer dl join layer l on l.id = dl.layer_id
                                where dl.document_id = d.id), '{}') as layers
               from document_version dv join document d on d.id = dv.document_id
               left join feature f on f.id = d.feature_id
               where not exists (select 1 from chunk c where c.document_version_id = dv.id and c.strategy_id = %s)""",
            (strategy_id,),
        )
        rows = await cur.fetchall()
    indexer = Indexer([strategy], embedder)
    for r in rows:
        await index_version(conn, indexer, r["id"], Doc(r["type"], r["body"], r["feature"], r["layers"]))
    return len(rows)


# --- search --------------------------------------------------------------------------------------


@dataclass
class Hit:
    document_id: int
    type: str
    feature: str | None
    section: str | None
    text: str
    score: float
    created_at: str
    ranks: dict = field(default_factory=dict)


_RRF_K = 60  # standard reciprocal-rank-fusion constant
# Below this cosine similarity a vector hit is noise: on confere-v2, questions unrelated to the project
# (kubernetes, a cake recipe) top out at 0.585 and related ones start at 0.64 (nomic-embed-text).
MIN_SIMILARITY = float(os.getenv("AUTONOME_SEARCH_MIN_SIMILARITY", "0.6"))
_CANDIDATES = 50


async def search(
    conn: AsyncConnection,
    query: str,
    project: str,
    strategy: Strategy,
    embedder: Embedder,
    *,
    feature: str | None = None,
    layer: str | None = None,
    doc_type: str | None = None,
    mode: SearchMode = "vector",  # measured best on shortlink: docs/benchmarks/retrieval.md
    per_document: bool = True,
    k: int = 8,
) -> list[Hit]:
    """Vector similarity and/or full-text over the latest version of each document, fused by
    reciprocal rank. Vector hits below MIN_SIMILARITY are dropped, so an empty answer means the memory
    has nothing on it. With per_document, only the best chunk of each document is returned
    (search small, return the parent)."""
    qvec = (await embedder.embed([query], "query"))[0] if mode != "fts" else None
    # Questions rarely contain every word of the answer: match any word and let ts_rank order them.
    words = re.findall(r"\w+", query)
    params = {
        "q": " or ".join(words) or query, "qvec": str(qvec) if qvec else None, "project": project, "strategy": strategy.name,
        "feature": feature, "layer": layer, "type": doc_type, "n": _CANDIDATES, "rrf": _RRF_K,
        "limit": k * 5 if per_document else k, "min_sim": MIN_SIMILARITY,
    }
    scope = """
        from chunk c
        join strategy s on s.id = c.strategy_id and s.name = %(strategy)s
        join document_version dv on dv.id = c.document_version_id
        join document d on d.id = dv.document_id
        join project p on p.id = d.project_id and p.slug = %(project)s
        left join feature f on f.id = d.feature_id
        where dv.version = (select max(version) from document_version where document_id = d.id)
          and (%(feature)s::text is null or f.slug = %(feature)s)
          and (%(type)s::text is null or d.type = %(type)s)
          and (%(layer)s::text is null or exists (
                select 1 from document_layer dl join layer l on l.id = dl.layer_id
                where dl.document_id = d.id and l.slug = %(layer)s))
    """
    vec = f"""
        select c.id, 'vector' as source, row_number() over (order by c.embedding <=> %(qvec)s::vector) as r
        {scope} and 1 - (c.embedding <=> %(qvec)s::vector) >= %(min_sim)s
        order by c.embedding <=> %(qvec)s::vector limit %(n)s
    """
    fts = f"""
        select c.id, 'fts' as source,
               row_number() over (order by ts_rank(c.tsv, websearch_to_tsquery('simple', %(q)s)) desc) as r
        {scope} and c.tsv @@ websearch_to_tsquery('simple', %(q)s)
        order by ts_rank(c.tsv, websearch_to_tsquery('simple', %(q)s)) desc limit %(n)s
    """
    ranked = {"hybrid": f"({vec}) union all ({fts})", "vector": vec, "fts": fts}[mode]
    sql = f"""
        with ranked as ({ranked}),
        fused as (
            select id, sum(1.0 / (%(rrf)s + r))::float8 as score, jsonb_object_agg(source, r) as ranks
            from ranked group by id
        )
        select d.id as document_id, d.type, f.slug as feature, c.section, c.text, fused.score,
               d.created_at::text as created_at, fused.ranks
        from fused
        join chunk c on c.id = fused.id
        join document_version dv on dv.id = c.document_version_id
        join document d on d.id = dv.document_id
        left join feature f on f.id = d.feature_id
        order by fused.score desc
        limit %(limit)s
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        hits = [Hit(**row) for row in await cur.fetchall()]
    if per_document:
        seen: set[int] = set()
        hits = [h for h in hits if not (h.document_id in seen or seen.add(h.document_id))]
    return hits[:k]


# --- gates ---------------------------------------------------------------------------------------


async def latest_of_type(conn: AsyncConnection, project: str, type: str) -> dict | None:
    """The latest version of the single document of this type in a project (e.g. its gate_config)."""
    docs = await list_documents(conn, project, type=type, limit=1)
    return docs[0] if docs else None


async def save_singleton(conn: AsyncConnection, indexer: Indexer | None, project: str, type: str, body: dict) -> dict:
    """Creates the project's document of this type or appends a version to it."""
    async with conn.transaction():
        current = await latest_of_type(conn, project, type)
        if current:
            await add_version(conn, indexer, current["id"], body)
            doc_id = current["id"]
        else:
            doc_id = await create_document(conn, indexer, project=project, type=type, body=body, status="active")
    return await get_document(conn, doc_id)


async def record_gate_runs(
    conn: AsyncConnection, project: str, session_id: str, runs: list[dict],
    task_id: int | None = None, config_version: int | None = None,
) -> None:
    project_id = await ensure_project(conn, project)
    async with conn.cursor() as cur:
        await cur.executemany(
            "insert into gate_run (project_id, session_id, task_id, config_version, gate, passed, duration_ms,"
            " signature, output) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(project_id, session_id, task_id, config_version, r["gate"], r["passed"], r["duration_ms"],
              r.get("signature"), redact(r.get("output", ""))[-4000:]) for r in runs],
        )


async def gate_runs(
    conn: AsyncConnection, *, project: str | None = None, session_id: str | None = None,
    task_id: int | None = None, limit: int = 500,
) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """select g.id, p.slug as project, g.session_id, g.task_id, g.config_version, g.gate, g.passed,
                      g.duration_ms, g.signature, g.output, g.created_at
               from gate_run g join project p on p.id = g.project_id
               where (%(project)s::text is null or p.slug = %(project)s)
                 and (%(session)s::text is null or g.session_id = %(session)s)
                 and (%(task)s::bigint is null or g.task_id = %(task)s)
               order by g.created_at desc limit %(limit)s""",
            {"project": project, "session": session_id, "task": task_id, "limit": limit},
        )
        return await cur.fetchall()


# --- live session events --------------------------------------------------------------------------

EVENTS_PER_TASK = 2000  # the most recent kept per task or plan; older ones are pruned
_EVENT_TEXT = 2000


async def add_events(conn: AsyncConnection, project: str, events: list[dict]) -> int:
    """Stores what running sessions are doing. Text is redacted like everything else and cut short."""
    if not events:
        return 0
    project_id = await ensure_project(conn, project)
    rows = [(project_id, e.get("task_id"), e.get("plan_id"), str(e.get("role", ""))[:40], str(e.get("kind", ""))[:20],
             redact(str(e.get("text", "")))[:_EVENT_TEXT], e.get("at")) for e in events]
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.executemany(
                "insert into task_event (project_id, task_id, plan_id, role, kind, text, at)"
                " values (%s, %s, %s, %s, %s, %s, coalesce(%s::timestamptz, now()))", rows)
        for column in ("task_id", "plan_id"):
            for owner in {r[1 if column == "task_id" else 2] for r in rows} - {None}:
                await conn.execute(
                    f"delete from task_event where {column} = %s and id < (select min(id) from (select id from task_event"
                    f" where {column} = %s order by id desc limit {EVENTS_PER_TASK}) recent)", (owner, owner))
    return len(rows)


async def events(conn: AsyncConnection, *, task_id: int | None = None, plan_id: int | None = None,
                 after: int = 0, limit: int = 500) -> list[dict]:
    column, owner = ("task_id", task_id) if task_id is not None else ("plan_id", plan_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"select id, role, kind, text, at from task_event where {column} = %s and id > %s"
                          f" order by id desc limit %s", (owner, after, limit))
        return list(reversed(await cur.fetchall()))


async def last_events(conn: AsyncConnection, task_ids: list[int]) -> dict[int, dict]:
    """The latest event of each task: what it is doing right now."""
    if not task_ids:
        return {}
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("select distinct on (task_id) task_id, role, kind, text, at from task_event"
                          " where task_id = any(%s) order by task_id, id desc", (task_ids,))
        return {r["task_id"]: r for r in await cur.fetchall()}


# --- conventions ---------------------------------------------------------------------------------


async def conventions_for(conn: AsyncConnection, project: str, layers: list[str] | None = None) -> list[dict]:
    """Active conventions that apply to any of these layers; conventions without layers apply to all."""
    docs = await list_documents(conn, project, type="convention", status="active")
    if layers is None:
        return docs
    return [d for d in docs if not d["layers"] or set(d["layers"]) & set(layers)]


# --- tasks ---------------------------------------------------------------------------------------


async def next_task(conn: AsyncConnection, project: str) -> dict | None:
    """Released tasks first (they only need merging), then the oldest pending task of an approved
    plan whose dependencies (tasks, and in a wave the plans it waits for) are done. Locks the row so
    two runners never take the same task."""
    row = await (await conn.execute(
        """select d.id from document d
           join project p on p.id = d.project_id and p.slug = %(project)s
           join lateral (select body from document_version v where v.document_id = d.id
                         order by v.version desc limit 1) dv on true
           join document plan on plan.id = (dv.body->>'plan_id')::bigint and plan.status = 'approved'
           where d.type = 'task' and d.status in ('released', 'pending')
             and not exists (
                 select 1 from jsonb_array_elements_text(coalesce(dv.body->'depends_on', '[]')) dep
                 join document dd on dd.id = dep::bigint where dd.status <> 'done')
             and not exists (
                 select 1 from jsonb_array_elements_text(coalesce(dv.body->'after_plans', '[]')) ap
                 join document pd on pd.id = ap::bigint where pd.status <> 'done')
           order by (d.status = 'released') desc, plan.id, (dv.body->>'seq')::int
           limit 1 for update of d skip locked""",
        {"project": project},
    )).fetchone()
    return await get_document(conn, row[0]) if row else None


async def review_flags(conn: AsyncConnection, project: str, feature: str | None) -> tuple[bool, bool]:
    """(fermata, playback) for a task of this feature, from the gate config's feature patterns."""
    doc = await latest_of_type(conn, project, "gate_config")
    if not doc or not feature:
        return False, False
    cfg = GateConfig.from_yaml(doc["body"]["yaml"])
    match = lambda patterns: any(fnmatch.fnmatch(feature, p) for p in patterns)
    return match(cfg.hold_before_merge.features), match(cfg.review_after_merge.features)


async def pending_gates(conn: AsyncConnection, project: str) -> list[dict]:
    """Gates of approved plans that join the gate config when their setup task merges (`with_task`)."""
    out = []
    for t in await list_documents(conn, project, type="task", limit=1000):
        if t["status"] in ("done", "abandoned") or not t["body"].get("adds_gates"):
            continue
        plan = await get_document(conn, t["body"]["plan_id"])
        if plan and plan["status"] == "approved":
            out += [{**g, "task_id": t["id"], "plan_id": plan["id"]} for g in t["body"]["adds_gates"]]
    return out


async def finish_plan_if_complete(conn: AsyncConnection, plan_id: int) -> bool:
    plan = await get_document(conn, plan_id)
    if plan and plan["body"].get("kind") == "maintenance":
        return False  # the standing plan cues add to; it ends only when a human abandons it
    open_tasks = await (await conn.execute(
        """select count(*) from link l join document t on t.id = l.to_id
           where l.from_id = %s and l.kind = 'has_task' and t.status not in ('done', 'abandoned')""",
        (plan_id,),
    )).fetchone()
    if open_tasks[0] == 0 and not await awaits_soundcheck(conn, plan):
        await conn.execute("update document set status = 'done' where id = %s and status = 'approved'", (plan_id,))
        return True
    return False


async def awaits_soundcheck(conn: AsyncConnection, plan: dict) -> bool:
    """With a soundcheck configured, a plan is done only once its feature passed one."""
    doc = await latest_of_type(conn, plan["project"], "gate_config")
    if not doc or GateConfig.from_yaml(doc["body"]["yaml"]).smoke_test is None:
        return False
    runs = plan["body"].get("soundchecks", [])
    return not (runs and runs[-1].get("passed"))


# --- living docs ---------------------------------------------------------------------------------


async def feature_doc(conn: AsyncConnection, project: str, feature: str | None) -> dict | None:
    docs = [d for d in await list_documents(conn, project, type="feature_doc", feature=feature, limit=500)
            if d["feature"] == feature]
    return docs[0] if docs else None


async def save_feature_doc(
    conn: AsyncConnection, indexer: Indexer | None, project: str, feature: str | None, body: dict,
    layers: list[str] | tuple = (), commit_sha: str | None = None,
) -> int:
    async with conn.transaction():
        current = await feature_doc(conn, project, feature)
        if current:
            await add_version(conn, indexer, current["id"], body, commit_sha)
            return current["id"]
        return await create_document(conn, indexer, project=project, type="feature_doc", body=body,
                                     feature=feature, layers=layers, status="active", commit_sha=commit_sha)


async def features(conn: AsyncConnection, project: str) -> list[dict]:
    """Every feature with its activity: the map of the project."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """select f.slug,
                      count(*) filter (where d.type = 'handoff') as handoffs,
                      count(*) filter (where d.type = 'task') as tasks,
                      count(*) filter (where d.type = 'decision' and d.status = 'accepted') as decisions,
                      count(*) filter (where d.type = 'feature_doc') > 0 as documented,
                      max(d.created_at) as last_activity,
                      max(d.created_at) filter (where d.type in ('handoff', 'task')) as last_work_at,
                      max(dv.created_at) filter (where d.type = 'feature_doc') as doc_updated_at,
                      avg((dv.body->>'adherence_score')::float8) filter (where d.type = 'handoff') as adherence
               from feature f join project p on p.id = f.project_id and p.slug = %s
               left join document d on d.feature_id = f.id
               left join lateral (select body, created_at from document_version v where v.document_id = d.id
                                  order by v.version desc limit 1) dv on true
               group by f.slug order by max(d.created_at) desc nulls last""",
            (project,),
        )
        return await cur.fetchall()


MAINTENANCE_PLAN = {
    "kind": "maintenance",
    "title": "Maintenance (cues)",
    "goal": "Tasks found by scheduled checks (cues): dependency updates, a red main, drift. Approving this"
            " plan once lets cues queue work here; abandon it to stop them.",
    "out_of_scope": ["anything not described in the task itself"],
    "tasks": [],
}


async def enqueue_task(
    conn: AsyncConnection, indexer: Indexer | None, project: str, *, title: str, spec: str,
    feature: str | None = None, layers: list[str] | tuple = (), source: str,
) -> dict:
    """Adds a task to the project's standing maintenance plan (created as a draft if missing). The same
    open source+title is not queued twice."""
    async with conn.transaction():
        plan = next((p for p in await list_documents(conn, project, type="plan")
                     if p["body"].get("kind") == "maintenance" and p["status"] in ("draft", "approved")), None)
        if plan is None:
            plan_id = await create_document(conn, indexer, project=project, type="plan", status="draft",
                                            body=MAINTENANCE_PLAN, feature="maintenance")
            plan = await get_document(conn, plan_id)
        existing = [l["id"] for l in await links_of(conn, plan["id"]) if l["kind"] == "has_task"]
        for task_id in existing:
            t = await get_document(conn, task_id)
            if t["status"] not in ("done", "abandoned") and (t["body"].get("source"), t["body"]["title"]) == (source, title):
                return {"task_id": task_id, "plan_id": plan["id"], "duplicate": True}
        task_id = await create_document(
            conn, indexer, project=project, type="task", status="pending", feature=feature, layers=layers,
            body={"title": title, "spec": spec, "acceptance": [], "plan_id": plan["id"], "seq": len(existing) + 1,
                  "depends_on": [], "fermata": False, "playback": (await review_flags(conn, project, feature))[1],
                  "source": source},
        )
        await link(conn, plan["id"], task_id, "has_task")
    return {"task_id": task_id, "plan_id": plan["id"], "plan_status": plan["status"], "duplicate": False}
