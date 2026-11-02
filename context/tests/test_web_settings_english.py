import re

from autonome_context.gates import FIELD_HELP, ROLE_GLOSS

from test_web_settings import YAML, _fields, bare, client, saved  # noqa: F401  (fixtures)

PORTUGUESE = ("Adicionar", "Versões", "Ligado", "vazio", "adicionado", "removido", "Máximo", "Papel", "Esforço",
              "Voltar", "Rodam", "Só slugs")


def _no_portuguese(text: str) -> None:
    assert [w for w in PORTUGUESE if w in text] == []


def test_every_settings_page_is_in_english(client, project, saved):
    saved(YAML)
    url = f"/ui/p/{project}/settings"
    review = client.post(url, data=_fields() | {"action": "review", "retries-max": "9"})
    pages = [client.get(url), client.get(f"{url}?view=yaml"), client.get(f"{url}/versions"),
             client.get(f"{url}/versions/1"), review]
    for page in pages:
        assert page.status_code == 200
        _no_portuguese(page.text)
    assert "+ Add gate" in pages[0].text and "Versions" in pages[0].text
    assert "Max retries" in pages[0].text and "Stop on repeat" in pages[0].text and "Task tokens" in pages[0].text
    assert "Review diffs" in pages[0].text


def test_the_gate_command_input_has_a_type_and_the_mono_class(client, project, saved):
    saved(YAML)
    text = client.get(f"/ui/p/{project}/settings").text
    tag = re.search(r'<input[^>]*name="gates-0-run"[^>]*>', text).group()
    assert 'type="text"' in tag and "mono" in tag


def test_an_invalid_draft_shows_its_errors_in_english(client, project, saved):
    saved(YAML)
    base = {k: v for k, v in _fields().items() if not k.startswith("gates-")}
    page = client.post(f"/ui/p/{project}/settings", data=base | {
        "action": "validate", "retries-max": "abc", "triage-model": "jev-latest", "triage-reject_at": "x",
        "budget-task_tokens": "zz", "gates-0-name": "lint", "gates-0-run": "", "gates-0-mode": "block",
        "gates-0-timeout_s": "900", "gates-1-name": "", "gates-1-run": "ls", "gates-1-mode": "block",
        "gates-1-timeout_s": "900"})
    assert page.status_code == 200
    _no_portuguese(page.text)
    for message in ("A whole number.", "A number, like 0.7.", "Not a valid number. Use 400000, 400k or 3M.",
                    "Required. A gate needs a command.", "Required. Give the gate a name."):
        assert message in page.text
    assert "problems prevent saving" in page.text


def test_the_first_setup_page_is_in_english(client, project, bare):
    page = client.get(f"/ui/p/{project}/settings")
    assert page.status_code == 200
    _no_portuguese(page.text)


def test_human_checkpoints_tell_hold_before_merge_from_review_after_merge(client, project, saved):
    saved(YAML)
    text = client.get(f"/ui/p/{project}/settings").text
    section = text[text.index('id="human-checkpoints"'):text.index('id="review"')]
    assert "Hold before merge" in section and "Stops before merge until you approve. Blocks the queue." in section
    assert "Review after merge" in section and "Merges, then you review later. Never blocks." in section
    assert ">Features<" not in section


def test_a_gate_with_three_globs_gets_a_three_row_when_box(client, project, saved):
    saved(YAML.replace('when: ["src/**"]', 'when: ["a/**", "b/**", "c/**"]'))
    text = client.get(f"/ui/p/{project}/settings").text
    assert re.search(r'<textarea name="gates-1-when" rows="3"', text)
    assert re.search(r'<textarea name="gates-0-when" rows="1"', text)


def test_allowed_commands_box_has_a_row_per_command_and_at_least_two(client, project, saved):
    saved(YAML)
    assert re.search(r'<textarea id="f-allowed_commands" name="allowed_commands" rows="2"', client.get(f"/ui/p/{project}/settings").text)
    saved(YAML + "allowed_commands: [uv, pnpm, node]\n")
    assert re.search(r'<textarea id="f-allowed_commands" name="allowed_commands" rows="3"', client.get(f"/ui/p/{project}/settings").text)


def test_triage_on_says_default_triage_off_and_offers_turn_off(client, project, saved):
    saved(YAML + "triage: {model: jev-latest}\n")
    text = client.get(f"/ui/p/{project}/settings").text
    assert "default: triage off" in text and ">turn off</button>" in text
    assert "default off" not in text.split('id="triage"')[1].split('id="smoke-test"')[0]


def test_versions_links_back_to_settings_and_has_no_back_button(client, project, saved):
    saved(YAML)
    for path in ("/versions", "/versions/1"):
        text = client.get(f"/ui/p/{project}/settings{path}").text
        assert f'href="/ui/p/{project}/settings">← Settings</a>' in text
        assert ">Back<" not in text


def test_reset_is_a_bordered_small_button_not_a_link(client, project, saved):
    saved(YAML)
    text = client.get(f"/ui/p/{project}/settings").text
    assert re.search(r'<button type="submit" class="btn btn--sm" name="reset" value="retries-max">reset</button>', text)


def test_help_texts_have_no_portuguese():
    for label, help_text in FIELD_HELP.values():
        _no_portuguese(label + " " + help_text)
    for gloss in ROLE_GLOSS.values():
        _no_portuguese(gloss)
