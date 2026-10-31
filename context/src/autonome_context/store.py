import gzip
import json
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .chunking import Strategy
from .embed import Embedder
from .handoff import Handoff
from .redact import redact


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


async def ensure_strategy(conn: AsyncConnection, strategy: Strategy, embedder: Embedder) -> int:
    """A strategy is identified by name; changing its params or model means a new name."""
    row = await (
        await conn.execute(
            "select id, embed_model, dims from strategy where name = %s", (strategy.name,)
        )
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


async def put_handoff(
    conn: AsyncConnection,
    h: Handoff,
    raw_transcript: str | None,
    strategy: Strategy,
    embedder: Embedder,
) -> int:
    """Stores a redacted handoff as a new document and indexes it. One transaction."""
    h = Handoff.model_validate(_redact_strings(h.model_dump()))
    async with conn.transaction():
        project_id = await ensure_project(conn, h.project)
        feature_id = None
        if h.feature:
            feature_id = await _id(
                conn,
                "insert into feature (project_id, slug, name) values (%s, %s, %s)"
                " on conflict (project_id, slug) do update set slug = excluded.slug returning id",
                (project_id, h.feature, h.feature),
            )
        doc_id = await _id(
            conn,
            "insert into document (project_id, feature_id, type, status) values (%s, %s, 'handoff', 'final')"
            " returning id",
            (project_id, feature_id),
        )
        for layer in h.layers:
            layer_id = await _id(
                conn,
                "insert into layer (project_id, slug) values (%s, %s)"
                " on conflict (project_id, slug) do update set slug = excluded.slug returning id",
                (project_id, layer),
            )
            await conn.execute(
                "insert into document_layer (document_id, layer_id) values (%s, %s)", (doc_id, layer_id)
            )
        version_id = await _id(
            conn,
            "insert into document_version (document_id, version, body, commit_sha) values (%s, 1, %s, %s)"
            " returning id",
            (doc_id, h.model_dump_json(), h.commit_sha),
        )
        if raw_transcript:
            await conn.execute(
                "insert into raw_transcript (document_id, session_id, content_gz) values (%s, %s, %s)",
                (doc_id, h.session_id, gzip.compress(redact(raw_transcript).encode())),
            )
        await index_version(conn, version_id, h, strategy, embedder)
    return doc_id


async def index_version(
    conn: AsyncConnection, version_id: int, h: Handoff, strategy: Strategy, embedder: Embedder
) -> int:
    chunks = strategy.chunk(h)
    if not chunks:
        return 0
    strategy_id = await ensure_strategy(conn, strategy, embedder)
    vectors = await embedder.embed([c.text for c in chunks], "document")
    async with conn.cursor() as cur:
        await cur.executemany(
            "insert into chunk (document_version_id, strategy_id, ord, section, text, embedding)"
            " values (%s, %s, %s, %s, %s, %s::vector)",
            [(version_id, strategy_id, c.ord, c.section, c.text, str(v)) for c, v in zip(chunks, vectors)],
        )
    return len(chunks)


@dataclass
class Hit:
    document_id: int
    feature: str | None
    section: str | None
    text: str
    score: float
    created_at: str


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
    k: int = 8,
) -> list[Hit]:
    """Hybrid search: vector similarity and full-text, fused by reciprocal rank."""
    [qvec] = await embedder.embed([query], "query")
    params = {
        "q": query,
        "qvec": str(qvec),
        "project": project,
        "strategy": strategy.name,
        "feature": feature,
        "layer": layer,
        "type": doc_type,
        "n": _CANDIDATES,
        "rrf": _RRF_K,
        "k": k,
    }
    scope = """
        from chunk c
        join strategy s on s.id = c.strategy_id and s.name = %(strategy)s
        join document_version dv on dv.id = c.document_version_id
        join document d on d.id = dv.document_id
        join project p on p.id = d.project_id and p.slug = %(project)s
        left join feature f on f.id = d.feature_id
        where (%(feature)s::text is null or f.slug = %(feature)s)
          and (%(type)s::text is null or d.type = %(type)s)
          and (%(layer)s::text is null or exists (
                select 1 from document_layer dl join layer l on l.id = dl.layer_id
                where dl.document_id = d.id and l.slug = %(layer)s))
    """
    sql = f"""
        with vec as (
            select c.id, row_number() over (order by c.embedding <=> %(qvec)s::vector) as r
            {scope}
            order by c.embedding <=> %(qvec)s::vector
            limit %(n)s
        ), fts as (
            select c.id, row_number() over (order by ts_rank(c.tsv, websearch_to_tsquery('simple', %(q)s)) desc) as r
            {scope}
              and c.tsv @@ websearch_to_tsquery('simple', %(q)s)
            order by ts_rank(c.tsv, websearch_to_tsquery('simple', %(q)s)) desc
            limit %(n)s
        ), fused as (
            select id, sum(1.0 / (%(rrf)s + r))::float8 as score
            from (select * from vec union all select * from fts) x
            group by id
        )
        select d.id as document_id, f.slug as feature, c.section, c.text, fused.score,
               d.created_at::text as created_at
        from fused
        join chunk c on c.id = fused.id
        join document_version dv on dv.id = c.document_version_id
        join document d on d.id = dv.document_id
        left join feature f on f.id = d.feature_id
        order by fused.score desc
        limit %(k)s
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return [Hit(**row) for row in await cur.fetchall()]
