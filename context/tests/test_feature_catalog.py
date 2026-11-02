import asyncio

import httpx
import psycopg
import pytest

from autonome_context import config, rules, store
from autonome_context.embed import OllamaEmbedder

from autonome_context import plans
from autonome_context.plans import PlanBody, TaskSpec

from conftest import FakeEmbedder, make_handoff

EMBEDDER = FakeEmbedder()
REAL_FEATURE_FOR = store.feature_for  # conftest swaps in a lenient one for the other test files


@pytest.fixture(autouse=True)
def real_feature_for(lenient_features, monkeypatch):
    monkeypatch.setattr(store, "feature_for", REAL_FEATURE_FOR)


def test_a_new_slug_without_a_description_is_refused_with_the_catalog(run_db, project):
    async def go(conn):
        await store.resolve_feature(conn, EMBEDDER, project, "wallet", "balance of the wallet ledger")
        with pytest.raises(rules.Forbidden) as e:
            await store.resolve_feature(conn, EMBEDDER, project, "signup", "   ")
        return str(e.value), await store.catalog(conn, project)

    message, catalog = run_db(go)
    assert "wallet: balance of the wallet ledger" in message
    assert [f["slug"] for f in catalog] == ["wallet"]


def test_a_known_slug_is_returned_as_it_is_and_keeps_its_description(run_db, project):
    async def go(conn):
        await store.resolve_feature(conn, EMBEDDER, project, "wallet", "balance of the wallet ledger", ["src/wallet/"])
        again = await store.resolve_feature(conn, EMBEDDER, project, "wallet", "something else entirely")
        return again, await store.catalog(conn, project)

    again, catalog = run_db(go)
    assert again == {"slug": "wallet", "created": False, "similar_to": None}
    assert catalog == [{"slug": "wallet", "description": "balance of the wallet ledger", "paths": ["src/wallet/"]}]


def test_the_slug_is_normalized_and_one_with_nothing_left_is_refused(run_db, project):
    async def go(conn):
        made = await store.resolve_feature(conn, EMBEDDER, project, "Short Links_v2", "shortened urls redirect")
        with pytest.raises(rules.Forbidden):
            await store.resolve_feature(conn, EMBEDDER, project, "--__--", "anything at all")
        return made

    assert run_db(go) == {"slug": "short-links-v2", "created": True, "similar_to": None}


def test_a_similar_description_returns_similar_to_and_creates_no_row(run_db, project):
    async def go(conn):
        await store.resolve_feature(conn, EMBEDDER, project, "models", "model and effort per agent role in config")
        r = await store.resolve_feature(conn, EMBEDDER, project, "models-config", "model and effort per agent role")
        return r, [f["slug"] for f in await store.catalog(conn, project)]

    r, slugs = run_db(go)
    assert r == {"slug": "models-config", "created": False, "similar_to": "models"}
    assert slugs == ["models"]


def test_a_feature_of_another_project_does_not_count(run_db, project):
    async def go(conn):
        await store.resolve_feature(conn, EMBEDDER, project + "-other", "models", "model and effort per agent role")
        return await store.resolve_feature(conn, EMBEDDER, project, "models", "model and effort per agent role")

    assert run_db(go)["created"] is True


def test_a_feature_embedded_by_another_model_is_left_out_of_the_comparison(run_db, project):
    class Other(FakeEmbedder):
        model = "other-model"

    async def go(conn):
        await store.resolve_feature(conn, Other(), project, "models", "model and effort per agent role")
        return await store.resolve_feature(conn, EMBEDDER, project, "models-config", "model and effort per agent role")

    assert run_db(go)["created"] is True


def test_two_equal_resolves_at_once_leave_one_row(db_url, project):
    async def one():
        async with await psycopg.AsyncConnection.connect(db_url) as conn:
            r = await store.resolve_feature(conn, EMBEDDER, project, "wallet", "balance of the wallet ledger")
            await conn.commit()
            return r

    async def go():
        return await asyncio.gather(one(), one())

    results = asyncio.run(go())
    assert sorted(r["created"] for r in results) == [False, True]

    async def count():
        async with await psycopg.AsyncConnection.connect(db_url) as conn:
            return await store.catalog(conn, project)

    assert len(asyncio.run(count())) == 1


def test_create_document_with_a_slug_outside_the_catalog_fails(run_db, project, indexer):
    async def go(conn):
        async with conn.transaction():
            with pytest.raises(rules.Forbidden):
                await store.create_document(conn, indexer, project=project, type="task", feature="ghost",
                                            body={"title": "t", "spec": "s"})
        return await store.catalog(conn, project)

    assert run_db(go) == []


def _plan():
    return PlanBody(title="W", goal="g", tasks=[TaskSpec(title="t", spec="s")])


def test_callers_without_a_description_cannot_create_a_feature(run_db, project, indexer):
    async def go(conn):
        async with conn.transaction():
            for call in (
                lambda: plans.draft(conn, indexer, project, "wallet", [], _plan()),
                lambda: store.put_handoff(conn, indexer, make_handoff(project, feature="wallet"), None),
            ):
                with pytest.raises(rules.Forbidden, match="not in the catalog"):
                    async with conn.transaction():  # a savepoint: the failed call leaves nothing behind
                        await call()
        return await store.catalog(conn, project)

    assert run_db(go) == []


def test_callers_file_work_under_a_catalog_feature(run_db, project, indexer):
    async def go(conn):
        await store.resolve_feature(conn, EMBEDDER, project, "wallet", "balance of the wallet ledger")
        await plans.draft(conn, indexer, project, "wallet", [], _plan())
        await store.put_handoff(conn, indexer, make_handoff(project, feature="wallet"), None)
        return await store.catalog(conn, project)

    assert [f["slug"] for f in run_db(go)] == ["wallet"]


def test_a_feature_too_similar_to_a_catalog_one_is_refused_naming_it(run_db, project, indexer):
    async def go(conn):
        await store.resolve_feature(conn, EMBEDDER, project, "models", "model and effort per agent role in config")
        with pytest.raises(rules.Forbidden, match="too similar to 'models'"):
            await store.feature_for(conn, indexer, project, "models-config", "model and effort per agent role")
        return [f["slug"] for f in await store.catalog(conn, project)]

    assert run_db(go) == ["models"]


def test_enqueue_task_creates_the_maintenance_feature_with_its_fixed_description(run_db, project, indexer):
    async def go(conn):
        await store.enqueue_task(conn, indexer, project, title="Fix main", spec="red", source="schedule:main-red")
        return await store.catalog(conn, project)

    [feature] = run_db(go)
    assert feature["slug"] == "maintenance" and feature["description"] == store.MAINTENANCE_FEATURE


# --- calibration of FEATURE_SIMILAR_AT with the real embedder -------------------------------------

DESCRIPTIONS = {
    "models": "Escolha do modelo e do esforço de cada papel de agente (implementer, reviewer, preflight, "
              "tester, retro) na configuração dos gates.",
    "models-config": "Configuração de modelo, esforço e modelo reserva de cada papel de agente, "
                     "com os padrões e as sobrescritas por linha de comando.",
    "review-loop": "O reviewer lê o diff de cada task antes do merge e, se achar problemas graves, "
                   "devolve a task ao implementer na mesma sessão.",
    "review-history": "Histórico das revisões do reviewer: achados por task, o que foi corrigido e o que "
                      "passou para a revisão depois do merge.",
    "runner": "O runner executa as tasks aprovadas, uma sessão e um worktree por task, e só faz merge na "
              "main quando os gates passam.",
    "triage": "Um classificador julga os desvios do plano que o handoff registrou e decide se aceita ou "
              "rejeita cada um.",
    "plans": "Planos escritos junto com o humano: objetivo, tasks, critérios de aceite e o que fica fora do "
             "escopo, aprovados antes de rodar.",
    "retro": "Retrospectiva automática depois de cada wave, que lê os insights e propõe convenções e no "
             "máximo um plano.",
    "gates": "Os gates rodam do mais barato ao mais caro, registram cada execução e viram task bloqueada "
             "quando falham repetidamente.",
    "ui": "Interface web do context, com as telas de aprovações, planos, features, busca e histórico do "
          "projeto.",
    "memory": "Memória do projeto: handoffs, decisões e documentos guardados, redigidos e indexados para "
              "busca por vetor.",
    "maintenance": "Manutenção do projeto: verificações agendadas que enfileiram tasks de atualização de "
                   "dependências e correção da main.",
}
ABOVE = [("models", "models-config"), ("review-loop", "review-history")]
BELOW = [("runner", "triage"), ("plans", "retro"), ("gates", "ui"), ("memory", "maintenance")]


def measure() -> dict[tuple[str, str], float]:
    """Cosine similarity of every pair above and below, with the real embedder; skips without Ollama."""
    settings = config.load()

    async def embed():
        embedder = OllamaEmbedder(settings.ollama_url, settings.embed_model, dims=0)
        return await embedder.embed(list(DESCRIPTIONS.values()), "document")

    try:
        vectors = asyncio.run(embed())
    except (httpx.HTTPError, OSError):
        pytest.skip("ollama not running")
    by_slug = dict(zip(DESCRIPTIONS, vectors))
    return {pair: store._cosine(by_slug[pair[0]], by_slug[pair[1]]) for pair in ABOVE + BELOW}


def test_feature_similarity_ollama():
    sims = measure()
    assert min(sims[p] for p in ABOVE) > store.FEATURE_SIMILAR_AT, sims
    assert max(sims[p] for p in BELOW) < store.FEATURE_SIMILAR_AT, sims
