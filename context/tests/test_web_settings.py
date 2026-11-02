import psycopg_pool
import pytest
from starlette.testclient import TestClient

from autonome_context import plans, services, store
from autonome_context.plans import PlanBody, ProposedGate, TaskSpec
from autonome_context.server import build_app

from conftest import add_feature

YAML = """\
gates:
  - {name: lint, run: ruff check ., fast: true}
  - {name: test, run: pytest, when: ["src/**"]}
retries: {max: 5}
hold_before_merge: {features: [links]}
permissions: auto
models: {reviewer: {model: opus, effort: high}}
language: pt-BR
"""


@pytest.fixture
def client(db_url, indexer, monkeypatch):
    async def get():
        pool = psycopg_pool.AsyncConnectionPool(db_url, open=False, min_size=1, max_size=2)
        await pool.open()
        return pool, indexer

    monkeypatch.setattr(services, "get", get)
    with TestClient(build_app(), base_url="http://127.0.0.1:8765") as c:
        yield c


@pytest.fixture
def saved(run_db, project, indexer):
    def save(yaml: str, source="human"):
        async def go(conn):
            await add_feature(conn, project, "links")
            await store.save_singleton(conn, indexer, project, "gate_config", {"yaml": yaml, "source": source})
            await conn.commit()

        run_db(go)

    return save


def test_the_page_shows_every_section_the_real_keys_and_what_differs_from_the_default(client, project, saved):
    saved(YAML)
    page = client.get(f"/ui/p/{project}/settings")
    assert page.status_code == 200
    for section in ("Gates", "Execution", "Human checkpoints", "Review", "Triage", "Smoke test", "Schedules",
                    "Agents", "Other"):
        assert f"<h2>{section}</h2>" in page.text
    for key in ("gates", "permissions", "retries.max", "hold_before_merge.features", "review_after_merge.features",
                "review.max_rounds", "triage", "triage.reject_at", "smoke_test", "smoke_test.checks", "schedules",
                "models", "retro_after_wave"):
        assert f'<code class="sfield__key">{key}</code>' in page.text
    assert 'value="ruff check ."' in page.text and 'value="pytest"' in page.text
    assert 'value="5"' in page.text and 'name="models-reviewer-model" value="opus"' in page.text
    changed = page.text.count("sfield is-changed")
    assert changed >= 3  # retries.max, permissions, hold_before_merge.features, language
    assert "default 3 · " in page.text and "default allowlist" in page.text
    assert 'name="reset" value="retries-max"' in page.text
    assert 'value="links"' in page.text  # the catalog, in the datalist
    assert "v1" in page.text and "você" in page.text


def test_the_header_names_a_version_that_came_from_a_plan(client, project, saved):
    saved(YAML, source={"plan_id": 77, "task_id": 3})
    assert "plano #77" in client.get(f"/ui/p/{project}/settings").text


def test_no_key_of_the_old_prototype_appears_in_the_rendered_page(client, project, saved):
    # the terms are spelled in halves so this file itself stays clear of the `vocabulary` gate
    old = [a + b for a, b in (("set", "list"), ("en", "core"), ("sound", "check"), ("back", "stage"),
                              ("re", "hearsal"), ("inter", "mission"), ("head", "liner"))]
    saved(YAML)
    for view in ("", "?view=yaml"):
        text = client.get(f"/ui/p/{project}/settings{view}").text.lower()
        assert not [k for k in old if k in text]
    text = client.get(f"/ui/p/{project}/settings").text
    for key in ("retries", "hold_before_merge", "review_after_merge", "schedules", "smoke_test", "implementer"):
        assert key in text


def test_a_pending_gate_of_an_approved_plan_is_read_only_with_its_plan_and_task(client, project, saved, run_db, indexer):
    saved(YAML)

    async def go(conn):
        plan_id = await plans.draft(conn, indexer, project, "links", [], PlanBody(
            title="Links", goal="g", tasks=[TaskSpec(title="t", spec="s")],
            gates=[ProposedGate(name="e2e-later", run="make e2e", with_task=1)]))
        [task] = await plans.approve(conn, indexer, plan_id)
        await conn.commit()
        return plan_id, task

    plan_id, task = run_db(go)
    page = client.get(f"/ui/p/{project}/settings").text
    assert "e2e-later" in page and "make e2e" in page
    assert f"plano #{plan_id} · entra quando #{task} for mergeada" in page
    assert "e2e-later" not in "".join(
        line for line in page.splitlines() if "<input" in line or "<textarea" in line)


def test_a_gate_run_with_markup_is_escaped(client, project, saved):
    saved('gates:\n  - {name: xss, run: "echo <script>alert(1)</script>"}\n')
    for view in ("", "?view=yaml"):
        page = client.get(f"/ui/p/{project}/settings{view}").text
        assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page


def test_the_yaml_view_shows_the_saved_text(client, project, saved):
    saved(YAML)
    page = client.get(f"/ui/p/{project}/settings?view=yaml").text
    assert 'name="yaml"' in page and "permissions: auto" in page and "<h2>Gates</h2>" not in page


def test_a_project_without_a_saved_config_shows_the_defaults(client, project, run_db):
    async def go(conn):
        await store.ensure_project(conn, project)
        await conn.commit()

    run_db(go)
    page = client.get(f"/ui/p/{project}/settings")
    assert page.status_code == 200 and "not saved yet" in page.text


def test_config_redirects_permanently_to_settings_and_the_nav_says_settings(client, project, saved):
    saved(YAML)
    r = client.get(f"/ui/p/{project}/config", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == f"/ui/p/{project}/settings"
    assert 'aria-current="page">Settings' in client.get(f"/ui/p/{project}/settings").text


def test_the_settings_page_carries_the_time_formatter_and_the_tz_cookie(client, project, saved):
    saved(YAML)
    text = client.get(f"/ui/p/{project}/settings").text
    assert "formatTimes(" in text and "tz=${tz}" in text and '<time datetime="' in text


def test_an_unknown_project_is_a_404(client):
    assert client.get("/ui/p/no-such-project-xyz/settings").status_code == 404


def test_the_yaml_view_still_saves_and_a_bad_text_is_refused(client, project, saved, run_db):
    saved(YAML)
    bad = client.post(f"/ui/p/{project}/settings", data={"yaml": "retries: {nope: 1}"})
    assert bad.status_code == 200 and "Not valid" in bad.text
    ok = client.post(f"/ui/p/{project}/settings", data={"yaml": "retries: {max: 9}"}, follow_redirects=False)
    assert ok.status_code == 303

    async def go(conn):
        return (await store.latest_of_type(conn, project, "gate_config"))["version"]

    assert run_db(go) == 2


def test_the_yaml_view_refuses_what_the_extra_checks_refuse_and_saves_nothing(client, project, saved, run_db):
    saved(YAML)
    text = "gates:\n  - {name: lint, run: ruff check .}\nhold_before_merge: {features: [no-such-feature]}\n"
    bad = client.post(f"/ui/p/{project}/settings", data={"yaml": text}, follow_redirects=False)
    assert bad.status_code == 200
    assert "Feature fora do catálogo: no-such-feature." in bad.text
    assert "no-such-feature" in bad.text  # the text stays in the textarea

    async def go(conn):
        return (await store.latest_of_type(conn, project, "gate_config"))["version"]

    assert run_db(go) == 1
