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


def test_help_texts_have_no_portuguese():
    for label, help_text in FIELD_HELP.values():
        _no_portuguese(label + " " + help_text)
    for gloss in ROLE_GLOSS.values():
        _no_portuguese(gloss)
