"""Server-rendered UI: the human side of the context (search, lab, documents)."""

import json
from pathlib import Path

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import evaluation, services, store
from .chunking import DEFAULT_STRATEGY, STRATEGIES

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=_HERE / "templates")
templates.env.filters["pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False, default=str)

DOC_TYPES = ["handoff", "plan", "task", "decision", "convention", "gate_config", "design"]
MODES = ["hybrid", "vector", "fts"]


def render(request: Request, name: str, **ctx) -> Response:
    project = request.path_params.get("project")
    return templates.TemplateResponse(request, name, {"project": project, **ctx})


async def home(request: Request) -> Response:
    pool, _ = await services.get()
    async with pool.connection() as conn:
        projects = await store.list_projects(conn)
    return render(request, "home.html", projects=projects)


async def search_page(request: Request) -> Response:
    p = request.query_params
    q = p.get("q", "").strip()
    mode = p.get("mode") if p.get("mode") in MODES else "hybrid"
    strategy = p.get("strategy") if p.get("strategy") in STRATEGIES else DEFAULT_STRATEGY
    hits = []
    if q:
        pool, indexer = await services.get()
        async with pool.connection() as conn:
            hits = await store.search(
                conn, q, request.path_params["project"], STRATEGIES[strategy], indexer.embedder,
                feature=p.get("feature") or None, layer=p.get("layer") or None,
                doc_type=p.get("type") or None, mode=mode, k=20,
            )
    return render(request, "search.html", q=q, mode=mode, strategy=strategy, hits=hits,
                  strategies=list(STRATEGIES), modes=MODES, types=DOC_TYPES, filters=p)


async def lab_page(request: Request) -> Response:
    project = request.path_params["project"]
    q = request.query_params.get("q", "").strip()
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        counts = {
            r[0]: r[1] for r in await (await conn.execute(
                "select s.name, count(c.id) filter (where p.slug = %s) from strategy s"
                " left join chunk c on c.strategy_id = s.id"
                " left join document_version dv on dv.id = c.document_version_id"
                " left join document d on d.id = dv.document_id"
                " left join project p on p.id = d.project_id group by s.name", (project,))).fetchall()
        }
        compare = {}
        if q:
            for name, strategy in STRATEGIES.items():
                compare[name] = await store.search(conn, q, project, strategy, indexer.embedder, k=5)
        questions = await evaluation.questions(conn, project)
        runs = await evaluation.runs(conn, project)
    strategies = [{"name": n, "params": s.params, "chunks": counts.get(n, 0)} for n, s in STRATEGIES.items()]
    return render(request, "lab.html", q=q, strategies=strategies, compare=compare,
                  questions=questions, runs=runs)


async def lab_eval(request: Request) -> Response:
    project = request.path_params["project"]
    k = int((await request.form()).get("k", 5))
    pool, indexer = await services.get()
    async with pool.connection() as conn:
        for strategy in STRATEGIES.values():
            for mode in MODES:
                await evaluation.run(conn, project, strategy, indexer.embedder, mode, k)
    return RedirectResponse(f"/ui/p/{project}/lab", status_code=303)


async def lab_question(request: Request) -> Response:
    project = request.path_params["project"]
    form = await request.form()
    expected = [int(x) for x in str(form.get("expected", "")).replace(",", " ").split() if x.isdigit()]
    if form.get("question") and expected:
        pool, _ = await services.get()
        async with pool.connection() as conn:
            await evaluation.add_question(conn, project, str(form["question"]), expected, "manual")
    return RedirectResponse(f"/ui/p/{project}/lab", status_code=303)


async def documents_page(request: Request) -> Response:
    project = request.path_params["project"]
    p = request.query_params
    pool, _ = await services.get()
    async with pool.connection() as conn:
        docs = await store.list_documents(conn, project, type=p.get("type") or None,
                                          status=p.get("status") or None, feature=p.get("feature") or None)
    return render(request, "documents.html", docs=docs, types=DOC_TYPES, filters=p)


async def document_page(request: Request) -> Response:
    pool, _ = await services.get()
    async with pool.connection() as conn:
        doc = await store.get_document(conn, int(request.path_params["doc_id"]))
        if not doc or doc["project"] != request.path_params["project"]:
            return Response("not found", status_code=404)
        links = await store.links_of(conn, doc["id"])
    return render(request, "document.html", doc=doc, links=links)


def routes() -> list:
    return [
        Route("/", lambda r: RedirectResponse("/ui")),
        Route("/ui", home),
        Route("/ui/p/{project}", lambda r: RedirectResponse(f"/ui/p/{r.path_params['project']}/search")),
        Route("/ui/p/{project}/search", search_page),
        Route("/ui/p/{project}/lab", lab_page),
        Route("/ui/p/{project}/lab/eval", lab_eval, methods=["POST"]),
        Route("/ui/p/{project}/lab/questions", lab_question, methods=["POST"]),
        Route("/ui/p/{project}/documents", documents_page),
        Route("/ui/p/{project}/documents/{doc_id:int}", document_page),
        Mount("/static", StaticFiles(directory=_HERE / "static"), name="static"),
    ]
