import gzip
import json
import re
from dataclasses import dataclass, field
from typing import Literal

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .chunking import Doc, Strategy
from .embed import Embedder
from .handoff import Handoff
from .redact import redact

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
        await index_version(conn, indexer, version_id, Doc(type, body, feature, list(layers)))
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
        await index_version(conn, indexer, version_id, Doc(doc["type"], body, doc["feature"], doc["layers"]))
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
    limit: int = 200,
) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _DOC_SELECT
            + """ where p.slug = %(project)s
                  and (%(type)s::text is null or d.type = %(type)s)
                  and (%(status)s::text is null or d.status = %(status)s)
                  and (%(feature)s::text is null or f.slug = %(feature)s)
                order by d.created_at desc limit %(limit)s""",
            {"project": project, "type": type, "status": status, "feature": feature, "limit": limit},
        )
        return await cur.fetchall()


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
                project=h.project, type="decision", body={"title": d.title, "decision": d.rationale},
                feature=h.feature, layers=h.layers, status="proposed",
            )
            await link(conn, doc_id, decision_id, "records")
        if raw_transcript:
            await conn.execute(
                "insert into raw_transcript (document_id, session_id, content_gz) values (%s, %s, %s)",
                (doc_id, h.session_id, gzip.compress(redact(raw_transcript).encode())),
            )
    return doc_id


# --- indexing ------------------------------------------------------------------------------------


async def index_version(conn: AsyncConnection, indexer: Indexer, version_id: int, doc: Doc) -> int:
    total = 0
    for strategy in indexer.strategies:
        chunks = strategy.chunk(doc)
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
    mode: SearchMode = "hybrid",
    per_document: bool = True,
    k: int = 8,
) -> list[Hit]:
    """Vector similarity and/or full-text over the latest version of each document, fused by
    reciprocal rank. With per_document, only the best chunk of each document is returned
    (search small, return the parent)."""
    qvec = (await embedder.embed([query], "query"))[0] if mode != "fts" else None
    # Questions rarely contain every word of the answer: match any word and let ts_rank order them.
    words = re.findall(r"\w+", query)
    params = {
        "q": " or ".join(words) or query, "qvec": str(qvec) if qvec else None, "project": project, "strategy": strategy.name,
        "feature": feature, "layer": layer, "type": doc_type, "n": _CANDIDATES, "rrf": _RRF_K,
        "limit": k * 5 if per_document else k,
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
        {scope}
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
    plan whose dependencies are done. Locks the row so two runners never take the same task."""
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
           order by (d.status = 'released') desc, plan.id, (dv.body->>'seq')::int
           limit 1 for update of d skip locked""",
        {"project": project},
    )).fetchone()
    return await get_document(conn, row[0]) if row else None


async def finish_plan_if_complete(conn: AsyncConnection, plan_id: int) -> bool:
    open_tasks = await (await conn.execute(
        """select count(*) from link l join document t on t.id = l.to_id
           where l.from_id = %s and l.kind = 'has_task' and t.status not in ('done', 'abandoned')""",
        (plan_id,),
    )).fetchone()
    if open_tasks[0] == 0:
        await conn.execute("update document set status = 'done' where id = %s and status = 'approved'", (plan_id,))
        return True
    return False


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
                      avg((dv.body->>'adherence_score')::float8) filter (where d.type = 'handoff') as adherence
               from feature f join project p on p.id = f.project_id and p.slug = %s
               left join document d on d.feature_id = f.id
               left join lateral (select body from document_version v where v.document_id = d.id
                                  order by v.version desc limit 1) dv on true
               group by f.slug order by max(d.created_at) desc nulls last""",
            (project,),
        )
        return await cur.fetchall()
