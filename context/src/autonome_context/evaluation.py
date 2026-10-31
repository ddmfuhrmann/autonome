"""Retrieval evaluation at document level: does the search bring back the documents that answer
the question, and how high? Relevance is binary (expected or not)."""

import json
import math
import os
import subprocess
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from . import store
from .chunking import Doc, Strategy, sections
from .embed import Embedder


@dataclass
class Metrics:
    recall: float
    mrr: float
    ndcg: float


def score(ranked: list[int], expected: set[int], k: int) -> Metrics:
    top = ranked[:k]
    hits = [i for i, doc in enumerate(top) if doc in expected]
    recall = len(hits) / len(expected) if expected else 0.0
    mrr = 1 / (hits[0] + 1) if hits else 0.0
    dcg = sum(1 / math.log2(i + 2) for i in hits)
    idcg = sum(1 / math.log2(i + 2) for i in range(min(len(expected), k)))
    return Metrics(recall, mrr, dcg / idcg if idcg else 0.0)


async def add_question(conn: AsyncConnection, project: str, question: str, expected: list[int], source: str) -> int:
    project_id = await store.ensure_project(conn, project)
    row = await (await conn.execute(
        "insert into eval_question (project_id, question, expected_document_ids, source)"
        " values (%s, %s, %s, %s) returning id",
        (project_id, question, expected, source),
    )).fetchone()
    return row[0]


async def questions(conn: AsyncConnection, project: str) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "select q.id, q.question, q.expected_document_ids, q.source from eval_question q"
            " join project p on p.id = q.project_id where p.slug = %s order by q.id",
            (project,),
        )
        return await cur.fetchall()


async def run(
    conn: AsyncConnection, project: str, strategy: Strategy, embedder: Embedder, mode: str = "hybrid", k: int = 5
) -> dict | None:
    qs = await questions(conn, project)
    if not qs:
        return None
    details, totals = [], Metrics(0, 0, 0)
    for q in qs:
        hits = await store.search(conn, q["question"], project, strategy, embedder, mode=mode, k=k)
        ranked = [h.document_id for h in hits]
        m = score(ranked, set(q["expected_document_ids"]), k)
        totals = Metrics(totals.recall + m.recall, totals.mrr + m.mrr, totals.ndcg + m.ndcg)
        details.append({"question_id": q["id"], "ranked": ranked, **m.__dict__})
    n = len(qs)
    result = {"recall": totals.recall / n, "mrr": totals.mrr / n, "ndcg": totals.ndcg / n}
    project_id = await store.ensure_project(conn, project)
    strategy_id = await store.ensure_strategy(conn, strategy, embedder)
    await conn.execute(
        "insert into eval_run (project_id, strategy_id, mode, k, n_questions, recall, mrr, ndcg, details)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (project_id, strategy_id, mode, k, n, result["recall"], result["mrr"], result["ndcg"], json.dumps(details)),
    )
    return {"strategy": strategy.name, "mode": mode, "k": k, "n": n, **result}


async def runs(conn: AsyncConnection, project: str, limit: int = 50) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "select r.id, s.name as strategy, s.embed_model, r.mode, r.k, r.n_questions, r.recall, r.mrr, r.ndcg,"
            " r.created_at from eval_run r join strategy s on s.id = r.strategy_id"
            " join project p on p.id = r.project_id where p.slug = %s order by r.created_at desc limit %s",
            (project, limit),
        )
        return await cur.fetchall()


_GENERATE_PROMPT = """You build an evaluation set for a search engine over a software project's memory.
Below is one document from that memory. Write {n} questions that a developer or an AI agent might
ask later and that this document answers. Rules: paraphrase, do not copy distinctive phrases from the
document; vary the angle (why, what went wrong, what was decided, what to watch out for); keep each
question under 25 words. The team asks in English and Portuguese: when writing more than one, write
one of them in Portuguese. Answer with ONLY a JSON array of strings.

Document ({type}, feature {feature}):
{text}
"""


def _ask_llm(prompt: str, model: str) -> list[str]:
    out = subprocess.run(
        ["claude", "-p", "--model", model, "--effort", "low", "--output-format", "json"],
        input=prompt, capture_output=True, text=True, timeout=300,
        env={**os.environ, "AUTONOME_HANDOFF_CHILD": "1"},
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr[-1000:])
    result = json.loads(out.stdout)["result"]
    return json.loads(result[result.find("["): result.rfind("]") + 1])


async def generate(conn: AsyncConnection, project: str, per_document: int = 2, model: str = "haiku") -> int:
    """Synthetic questions for every handoff and decision that has none yet."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """select d.id from document d join project p on p.id = d.project_id
               where p.slug = %s and d.type in ('handoff', 'decision')
                 and not exists (select 1 from eval_question q where d.id = any(q.expected_document_ids))""",
            (project,),
        )
        ids = [r["id"] for r in await cur.fetchall()]
    created = 0
    for doc_id in ids:
        doc = await store.get_document(conn, doc_id)
        text = "\n".join(f"{n}: {t}" for n, t in sections(Doc(doc["type"], doc["body"], doc["feature"], doc["layers"])))
        prompt = _GENERATE_PROMPT.format(n=per_document, type=doc["type"], feature=doc["feature"], text=text[:6000])
        # A handoff and the decisions it recorded say the same thing; either one answers.
        expected = [doc_id] + [l["id"] for l in await store.links_of(conn, doc_id) if l["kind"] == "records"]
        for question in _ask_llm(prompt, model)[:per_document]:
            await add_question(conn, project, question, expected, "synthetic")
            created += 1
    return created
