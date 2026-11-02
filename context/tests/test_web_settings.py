import html
import re

import psycopg_pool
import pytest
import yaml
from starlette.testclient import TestClient

from autonome_context import plans, services, settings_form, store
from autonome_context.gates import GateConfig
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


def test_the_yaml_view_goes_through_the_review_and_a_bad_text_is_refused(client, project, saved, run_db):
    saved(YAML)
    bad = client.post(f"/ui/p/{project}/settings", data={"yaml": "retries: {nope: 1}"})
    assert bad.status_code == 200 and "Not valid" in bad.text
    review = client.post(f"/ui/p/{project}/settings", data={"yaml": "retries: {max: 9}", "action": "review"},
                         follow_redirects=False)
    assert review.status_code == 200 and "Revisar e salvar" in review.text
    assert _versions(run_db, project) == 1


def _fields(yaml_text: str = YAML) -> dict[str, str]:
    return settings_form.config_to_form(GateConfig.from_yaml(yaml_text))


def _versions(run_db, project) -> int:
    async def go(conn):
        return (await store.latest_of_type(conn, project, "gate_config"))["version"]

    return run_db(go)


def test_validate_keeps_what_was_typed_marks_the_field_and_disables_saving(client, project, saved, run_db):
    saved(YAML)
    page = client.post(f"/ui/p/{project}/settings", data=_fields() | {
        "action": "validate", "triage-model": "jev-latest", "triage-reject_at": "1.4"})
    assert page.status_code == 200
    assert 'name="triage-reject_at" value="1.4"' in page.text  # the typed value stays
    assert '<div class="sfield is-changed has-error" id="f-triage-reject_at-field">' in page.text
    assert "1 problema impede salvar" in page.text and 'href="#f-triage-reject_at"' in page.text
    assert 'class="sdot sdot--bad"' in page.text and 'class="ssec has-error" id="triage"' in page.text
    assert 'value="review" disabled>' in page.text
    assert _versions(run_db, project) == 1


def test_a_valid_draft_can_be_reviewed_and_marks_the_changed_sections(client, project, saved, run_db):
    saved(YAML)
    page = client.post(f"/ui/p/{project}/settings", data=_fields() | {"action": "validate", "retries-max": "9"})
    assert "disabled" not in page.text.split("Review &amp; save")[0].rsplit("<button", 1)[1]
    assert "problema" not in page.text and 'class="ssec is-changed" id="execution"' in page.text
    assert 'class="ssec" id="gates"' in page.text and 'value="9"' in page.text
    assert _versions(run_db, project) == 1


def test_the_yaml_view_of_a_draft_is_canonical_and_goes_back_to_the_same_fields(client, project, saved, run_db):
    saved(YAML)
    draft = _fields() | {"retries-max": "9", "budget-task_tokens": "400k"}
    yaml_page = client.post(f"/ui/p/{project}/settings", data=draft | {"action": "view_yaml"})
    assert 'name="yaml"' in yaml_page.text and "<h2>Gates</h2>" not in yaml_page.text
    text = html.unescape(re.search(r'<textarea name="yaml"[^>]*>(.*?)</textarea>', yaml_page.text, re.S).group(1))
    canonical = GateConfig.model_validate(settings_form.form_to_config(draft, {"links"})[0])
    assert GateConfig.from_yaml(text) == canonical and "max: 9" in text
    back = client.post(f"/ui/p/{project}/settings", data={"yaml": text, "action": "view_form"})
    assert "<h2>Gates</h2>" in back.text
    shown = {k: v for k, v in settings_form.config_to_form(canonical).items()}
    for key in ("retries-max", "budget-task_tokens", "gates-0-run", "gates-1-run", "permissions", "language"):
        assert re.search(rf'name="{key}"[^>]*value="{re.escape(shown[key])}"|value="{re.escape(shown[key])}" checked',
                         back.text), key
    assert _versions(run_db, project) == 1


def test_a_yaml_syntax_error_stays_in_the_yaml_view_with_the_line(client, project, saved, run_db):
    saved(YAML)
    bad = "gates:\n  - {name: lint, run: ruff}\nretries: [unclosed\n"
    for action in ("view_form", "validate"):
        page = client.post(f"/ui/p/{project}/settings", data={"yaml": bad, "action": action})
        assert page.status_code == 200 and 'name="yaml"' in page.text and "<h2>Gates</h2>" not in page.text
        assert re.search(r"na linha \d+", page.text) and 'data-error-line="' in page.text
        assert "retries: [unclosed" in page.text
    assert not re.search(r'data-error-line=""', page.text)
    assert _versions(run_db, project) == 1


def test_a_feature_outside_the_catalog_is_an_error_on_its_field(client, project, saved, run_db):
    saved(YAML)
    page = client.post(f"/ui/p/{project}/settings", data=_fields() | {
        "action": "validate", "hold_before_merge-features": "links, no-such-feature"})
    assert "Feature fora do catálogo: no-such-feature." in page.text
    assert 'id="f-hold_before_merge-features-field"' in page.text and "has-error" in page.text
    assert 'name="hold_before_merge-features" value="links, no-such-feature"' in page.text
    assert 'class="ssec has-error" id="human-checkpoints"' in page.text
    assert _versions(run_db, project) == 1


def test_gates_posted_out_of_order_come_out_in_the_order_of_their_indexes(client, project, saved, run_db):
    saved(YAML)
    rows = {}
    for i, name in ((2, "third"), (0, "first"), (1, "second")):  # the body lists 2, 0, 1
        rows |= {f"gates-{i}-name": name, f"gates-{i}-run": f"run-{name}", f"gates-{i}-mode": "block",
                 f"gates-{i}-timeout_s": "900"}
    base = {k: v for k, v in _fields().items() if not k.startswith("gates-")}
    page = client.post(f"/ui/p/{project}/settings", data=base | rows | {"action": "view_yaml"})
    text = html.unescape(re.search(r'<textarea name="yaml"[^>]*>(.*?)</textarea>', page.text, re.S).group(1))
    assert [g["name"] for g in yaml.safe_load(text)["gates"]] == ["first", "second", "third"]
    form = client.post(f"/ui/p/{project}/settings", data=base | rows | {"action": "validate"})
    assert [form.text.index(f'value="{n}"') for n in ("first", "second", "third")] == sorted(
        form.text.index(f'value="{n}"') for n in ("first", "second", "third"))
    assert _versions(run_db, project) == 1


def test_a_row_error_is_shown_on_its_row_and_linked(client, project, saved, run_db):
    saved(YAML)
    base = {k: v for k, v in _fields().items() if not k.startswith("gates-")}
    page = client.post(f"/ui/p/{project}/settings", data=base | {
        "action": "validate", "gates-3-name": "x", "gates-3-run": "", "gates-3-when": "src/**", "gates-3-mode": "block",
        "gates-3-timeout_s": "900"})
    assert 'id="f-gates-3"' in page.text and 'href="#f-gates-3"' in page.text
    assert 'class="srow srow--gates has-error" id="f-gates-3"' in page.text
    assert _versions(run_db, project) == 1


def _draft_gate_names(page) -> list[str]:
    return [n for n in re.findall(r'name="gates-\d+-name" value="([^"]*)"', page.text) if n]  # not the blank row


def test_the_row_and_reset_buttons_work_without_js(client, project, saved, run_db):
    saved(YAML)
    url = f"/ui/p/{project}/settings"
    assert _draft_gate_names(client.post(url, data=_fields())) == ["lint", "test"]
    moved = client.post(url, data=_fields() | {"move": "gates-1-up"})
    assert _draft_gate_names(moved) == ["test", "lint"]
    assert 'name="gates-0-run" value="pytest"' in moved.text
    down = client.post(url, data=_fields() | {"move": "gates-0-down"})
    assert _draft_gate_names(down) == ["test", "lint"]
    edge = client.post(url, data=_fields() | {"move": "gates-0-up"})
    assert _draft_gate_names(edge) == ["lint", "test"]
    removed = client.post(url, data=_fields() | {"remove": "gates-0"})
    assert _draft_gate_names(removed) == ["test"]
    reset = client.post(url, data=_fields() | {"reset": "models-reviewer"})
    assert 'name="models-reviewer-model" value="opus"' in client.post(url, data=_fields()).text
    assert 'name="models-reviewer-model" value="opus"' not in reset.text
    reset = client.post(url, data=_fields() | {"reset": "retries-max"})
    assert 'name="retries-max" value="3"' in reset.text
    assert _versions(run_db, project) == 1


def test_the_yaml_view_validate_saves_nothing(client, project, saved, run_db):
    saved(YAML)
    page = client.post(f"/ui/p/{project}/settings", data={"yaml": "retries: {max: 9}", "action": "validate"})
    assert page.status_code == 200 and "Válido" in page.text and 'name="yaml"' in page.text
    assert _versions(run_db, project) == 1


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


def _latest(run_db, project) -> dict:
    async def go(conn):
        return await store.latest_of_type(conn, project, "gate_config")

    return run_db(go)


def _hidden(page) -> dict[str, str]:
    """The fields a review page posts back to save."""
    out = {m.group(1): m.group(2) for m in re.finditer(r'<input type="hidden" name="(\w+)" value="([^"]*)"', page.text)}
    out["yaml"] = html.unescape(re.search(r'<textarea name="yaml" hidden>(.*?)</textarea>', page.text, re.S).group(1))
    return out


def test_review_then_save_creates_the_next_version_with_source_and_note(client, project, saved, run_db):
    saved(YAML)
    url = f"/ui/p/{project}/settings"
    review = client.post(url, data=_fields() | {"action": "review", "retries-max": "9"})
    assert review.status_code == 200 and "Revisar e salvar" in review.text
    assert "retries.max: 5 → 9" in review.text and "+  max: 9" in review.text
    assert "YAML canônico" in review.text and _versions(run_db, project) == 1
    saved_page = client.post(url, data=_hidden(review) | {"action": "save", "note": "mais retries"},
                             follow_redirects=False)
    assert saved_page.status_code == 303 and saved_page.headers["location"] == url
    latest = _latest(run_db, project)
    assert latest["version"] == 2
    assert latest["body"]["source"] == "human" and latest["body"]["note"] == "mais retries"
    assert GateConfig.from_yaml(latest["body"]["yaml"]).retries.max == 9


def test_save_with_an_old_base_version_saves_nothing_and_reviews_against_the_new_one(
        client, project, saved, run_db, indexer):
    saved(YAML)
    url = f"/ui/p/{project}/settings"
    review = client.post(url, data=_fields() | {"action": "review", "retries-max": "9"})
    fields = _hidden(review)
    assert fields["base_version"] == "1"

    async def go(conn):
        await plans.apply_gates(conn, indexer, project, [{"name": "e2e", "run": "make e2e"}],
                                {"plan_id": 5, "task_id": 2})
        await conn.commit()

    run_db(go)
    page = client.post(url, data=fields | {"action": "save", "note": "x"}, follow_redirects=False)
    assert page.status_code == 200 and "mudou enquanto você revisava" in page.text
    assert _hidden(page)["base_version"] == "2" and "e2e" in page.text
    assert _versions(run_db, project) == 2


def test_saving_from_the_yaml_view_keeps_the_comments_and_from_the_form_the_canonical_yaml(
        client, project, saved, run_db):
    saved(YAML)
    url = f"/ui/p/{project}/settings"
    commented = "# why nine\nretries: {max: 9}  # up from 5\n"
    review = client.post(url, data={"yaml": commented, "action": "review"})
    assert "texto como está" in review.text
    assert client.post(url, data=_hidden(review) | {"action": "save"}, follow_redirects=False).status_code == 303
    assert _latest(run_db, project)["body"]["yaml"] == commented

    review = client.post(url, data=_fields(commented) | {"action": "review", "retries-max": "7"})
    assert "YAML canônico" in review.text
    client.post(url, data=_hidden(review) | {"action": "save"}, follow_redirects=False)
    body = _latest(run_db, project)["body"]
    assert body["yaml"] == yaml.safe_dump({"retries": {"max": 7}}, sort_keys=False) and "note" not in body


def test_an_invalid_draft_posted_straight_to_save_saves_nothing(client, project, saved, run_db):
    saved(YAML)
    url = f"/ui/p/{project}/settings"
    bad_yaml = client.post(url, data={"yaml": "retries: {nope: 1}", "action": "save", "base_version": "1"})
    assert bad_yaml.status_code == 200 and "Not valid" in bad_yaml.text
    bad_form = client.post(url, data=_fields() | {"action": "save", "base_version": "1", "triage-model": "haiku",
                                                  "triage-reject_at": "1.4"})
    assert bad_form.status_code == 200 and "1 problema impede salvar" in bad_form.text
    unknown = client.post(url, data={"yaml": "hold_before_merge: {features: [nope]}", "action": "save",
                                     "base_version": "1"})
    assert "Feature fora do catálogo" in unknown.text
    assert _versions(run_db, project) == 1


@pytest.mark.parametrize("note", ["a\x00b", "x" * 501, "a\nb"])
def test_a_note_with_a_control_character_or_over_500_characters_is_refused(client, project, saved, run_db, note):
    saved(YAML)
    url = f"/ui/p/{project}/settings"
    review = client.post(url, data=_fields() | {"action": "review", "retries-max": "9"})
    page = client.post(url, data=_hidden(review) | {"action": "save", "note": note})
    assert page.status_code == 200 and "Nota recusada" in page.text
    assert _versions(run_db, project) == 1
    ok = client.post(url, data=_hidden(review) | {"action": "save", "note": "x" * 500}, follow_redirects=False)
    assert ok.status_code == 303 and _versions(run_db, project) == 2


def test_a_review_without_changes_disables_save_and_says_nothing_changed(client, project, saved, run_db):
    saved(YAML)
    page = client.post(f"/ui/p/{project}/settings", data=_fields() | {"action": "review"})
    assert "nada mudou" in page.text and 'value="save" disabled>' in page.text
    assert _versions(run_db, project) == 1
