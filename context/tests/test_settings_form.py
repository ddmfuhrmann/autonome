import types
import typing

import pytest
from pydantic import BaseModel

from autonome_context.gates import DEFAULT_MODELS, FIELD_HELP, ROLE_GLOSS, GateConfig
from autonome_context.settings_form import config_to_form, extra_checks, field_changes, form_to_config

CATALOG = {"ui", "memory"}
BASE = config_to_form(GateConfig())  # a checkbox absent from a form is off, so partial forms start from this


def roundtrip(cfg: GateConfig):
    form = config_to_form(cfg)
    data, errors = form_to_config(form, CATALOG)
    assert errors == {}
    back = GateConfig.model_validate(data)
    assert back == cfg
    assert config_to_form(back) == form
    return data


def test_defaults_give_an_empty_config():
    data, errors = form_to_config(config_to_form(GateConfig()), CATALOG)
    assert (data, errors) == ({}, {})


def test_gates_roundtrip_keep_order():
    cfg = GateConfig.model_validate({"gates": [
        {"name": "lint", "run": "ruff check .", "fast": True, "when": ["src/**", "tests/**"]},
        {"name": "test", "run": "pytest", "mode": "fix", "timeout_s": 300},
        {"name": "a", "run": "true"},
    ]})
    data = roundtrip(cfg)
    assert [g["name"] for g in data["gates"]] == ["lint", "test", "a"]
    assert data["gates"][2] == {"name": "a", "run": "true"}


def test_gate_order_follows_index_not_key_order():
    form = {"gates-1-name": "b", "gates-1-run": "y", "gates-0-name": "a", "gates-0-run": "x"}
    data, errors = form_to_config(form, CATALOG)
    assert errors == {}
    assert [g["name"] for g in data["gates"]] == ["a", "b"]


@pytest.mark.parametrize("raw", [
    {"retries": {"max": 5, "stop_on_repeat": False}},
    {"budget": {"task_tokens": 123, "task_minutes": 10, "plan_tokens": 999}},
    {"allowed_commands": ["./mvnw", "make"]},
    {"interactive": True},
    {"permissions": "auto"},
    {"hold_before_merge": {"features": ["ui"]}, "review_after_merge": {"features": ["memory", "ui"]}},
    {"task_size": {"max_lines": 150}},
    {"merge": {"mode": "pr"}},
    {"merge": {"mode": "pr", "pr": "auto"}},
    {"review": {"enabled": False, "max_rounds": 4}},
    {"triage": {"model": "haiku", "fallback": "off", "reject_at": 0.5, "max_rounds": 3}},
    {"triage": {}},
    {"smoke_test": {"up": "up", "down": "down", "url": "http://x", "seed": "seed", "tester": False,
                    "max_fixes": 1, "checks": [{"name": "e2e", "run": "./e2e", "timeout_s": 60}, {"name": "b", "run": "b"}]}},
    {"schedules": [{"name": "drift", "kind": "doc-drift", "every_hours": 12},
                   {"name": "cmd", "kind": "command", "run": "echo", "task": "Fix it"}]},
    {"schedules": [{"name": "simp", "kind": "simplify", "max_tasks": 5}, {"name": "simp3", "kind": "simplify"}]},
    {"models": {"reviewer": {"model": "opus", "effort": "high", "fallback": "sonnet"}}},
    {"models": {"simplifier": {"model": "sonnet", "effort": "medium"}}},
    {"retro_after_wave": False},
    {"language": "pt-BR"},
])
def test_section_roundtrip(raw):
    roundtrip(GateConfig.model_validate(raw))


def test_max_tasks_errors_land_on_the_form_field():
    form = {"schedules-2-name": "c", "schedules-2-kind": "command", "schedules-2-run": "x", "schedules-2-max_tasks": "4"}
    _, errors = form_to_config(form, CATALOG)
    assert "schedules-2-max_tasks" in errors
    form = {"schedules-0-name": "s", "schedules-0-kind": "simplify", "schedules-0-max_tasks": "11"}
    _, errors = form_to_config(form, CATALOG)
    assert "schedules-0-max_tasks" in errors


def test_only_differing_roles_are_written():
    cfg = GateConfig.model_validate({"models": {"docs": {"model": "haiku", "effort": "low"}}})
    data = roundtrip(cfg)
    assert data == {"models": {"docs": {"model": "haiku", "effort": "low"}}}


def test_budget_units_and_bad_numbers():
    data, errors = form_to_config(BASE | {"budget-task_tokens": "400k", "budget-plan_tokens": "3M"}, CATALOG)
    assert (data, errors) == ({}, {})  # both are the defaults
    data, errors = form_to_config(BASE | {"budget-task_tokens": "500k", "budget-plan_tokens": "2M"}, CATALOG)
    assert errors == {}
    assert data["budget"] == {"task_tokens": 500_000, "plan_tokens": 2_000_000}

    _, errors = form_to_config(BASE | {"budget-task_tokens": "abc", "budget-task_minutes": "-5"}, CATALOG)
    assert set(errors) == {"budget-task_tokens", "budget-task_minutes"}


@pytest.mark.parametrize("text", ["infk", "1e999k", "1e400M", "nank", "k", "M"])
def test_malformed_budget_with_unit_is_a_field_error(text):
    data, errors = form_to_config(BASE | {"budget-task_tokens": text}, CATALOG)
    assert list(errors) == ["budget-task_tokens"]
    assert data == {} or "budget" not in data


def test_task_size_default_is_not_written_and_low_values_are_errors():
    data, errors = form_to_config(BASE | {"task_size-max_lines": "300"}, CATALOG)
    assert (data, errors) == ({}, {})
    data, errors = form_to_config(BASE | {"task_size-max_lines": "200"}, CATALOG)
    assert (data, errors) == ({"task_size": {"max_lines": 200}}, {})
    _, errors = form_to_config(BASE | {"task_size-max_lines": "10"}, CATALOG)
    assert list(errors) == ["task_size-max_lines"]


def test_merge_defaults_are_not_written_and_bad_values_are_errors():
    assert form_to_config(BASE | {"merge-mode": "ff", "merge-pr": "manual"}, CATALOG) == ({}, {})
    data, errors = form_to_config(BASE | {"merge-mode": "pr", "merge-pr": "auto"}, CATALOG)
    assert (data, errors) == ({"merge": {"mode": "pr", "pr": "auto"}}, {})
    _, errors = form_to_config(BASE | {"merge-mode": "squash"}, CATALOG)
    assert list(errors) == ["merge-mode"]


def test_triage_errors_and_off():
    _, errors = form_to_config(BASE | {"triage-model": "jev-latest", "triage-reject_at": "1.4"}, CATALOG)
    assert list(errors) == ["triage-reject_at"]
    data, errors = form_to_config(BASE | {"triage-model": "off", "triage-reject_at": "1.4"}, CATALOG)
    assert (data, errors) == ({}, {})


def test_smoke_test_block_needs_up_down_or_url():
    assert form_to_config(BASE | {"smoke_test-seed": "s"}, CATALOG) == ({}, {})
    _, errors = form_to_config(BASE | {"smoke_test-up": "up"}, CATALOG)
    assert set(errors) == {"smoke_test-down", "smoke_test-url"}


def test_pydantic_errors_use_form_index():
    form = {"gates-0-name": "", "gates-0-run": "", "gates-3-name": "x", "gates-3-run": "y", "gates-3-timeout_s": "5",
            "gates-3-mode": "weird"}
    _, errors = form_to_config(form, CATALOG)
    assert "gates-3-mode" in errors


def test_extra_checks_each():
    cfg = GateConfig.model_validate({
        "gates": [{"name": "a", "run": ""}, {"name": "a", "run": "x"}, {"name": "b\x00", "run": "x"},
                  {"name": "", "run": "x"}],
        "schedules": [{"name": "s", "kind": "command"}, {"name": "d", "kind": "doc-drift"}],
        "hold_before_merge": {"features": ["ui", "nope"]},
        "review_after_merge": {"features": ["ghost"]},
        "budget": {"task_tokens": 0},
    })
    errors = extra_checks(cfg, CATALOG)
    assert errors["gates-0-run"] == "Required. A gate needs a command."
    assert "Duplicate name" in errors["gates-1-name"]
    assert "control character" in errors["gates-2-name"]
    assert "gates-3-name" in errors
    assert "schedules-0-run" in errors and "schedules-1-run" not in errors
    assert "nope" in errors["hold_before_merge-features"] and "ui" not in errors["hold_before_merge-features"]
    assert "ghost" in errors["review_after_merge-features"]
    assert "budget-task_tokens" in errors
    assert extra_checks(GateConfig(), CATALOG) == {}


def test_form_to_config_reports_extra_checks_at_form_index():
    form = {"gates-0-name": "", "gates-0-run": "", "gates-2-name": "a", "gates-2-run": ""}
    _, errors = form_to_config(form, CATALOG)
    assert "gates-2-run" in errors  # the form's index, not the config's
    _, errors = form_to_config({"hold_before_merge-features": ["zzz"]}, CATALOG)
    assert "hold_before_merge-features" in errors


def test_field_changes_gates():
    old = GateConfig.model_validate({"gates": [{"name": "pytest", "run": "pytest", "timeout_s": 900},
                                               {"name": "old", "run": "x"}]})
    new = GateConfig.model_validate({"gates": [{"name": "pytest", "run": "pytest", "timeout_s": 300},
                                               {"name": "migrations", "run": "m"}]})
    changes = field_changes(old, new)
    assert "Gate pytest: timeout 900 → 300" in changes
    assert "Gate migrations added" in changes
    assert "Gate old removed" in changes
    assert field_changes(new, new) == []


def test_field_changes_in_english():
    old = GateConfig.model_validate({"budget": {"task_tokens": 400000}})
    new = GateConfig.model_validate({"budget": {"task_tokens": 1000000},
                                     "gates": [{"name": "plugin-version", "run": "x"}]})
    changes = field_changes(old, new)
    assert "Gate plugin-version added" in changes
    assert "budget.task_tokens: 400000 → 1000000" in changes
    assert "Gate plugin-version removed" in field_changes(new, old)
    with_triage = GateConfig.model_validate({"triage": {"reject_at": 0.5}})
    assert any(c.endswith("(new)") for c in field_changes(GateConfig(), with_triage))
    assert any(c.endswith("→ removed") for c in field_changes(with_triage, GateConfig()))


def test_field_changes_other_fields():
    old = GateConfig()
    new = GateConfig.model_validate({"permissions": "auto", "triage": {}, "retro_after_wave": False,
                                     "models": {"docs": {"model": "haiku", "effort": "low"}}})
    changes = field_changes(old, new)
    assert "permissions: allowlist → auto" in changes
    assert "retro_after_wave: yes → no" in changes
    assert any(c.startswith("triage.model: jev-latest") for c in changes)
    assert "Model docs: model: sonnet → haiku" in changes
    assert field_changes(None, GateConfig()) == []


def _described(model: type[BaseModel], path: str = ""):
    for name, field in model.model_fields.items():
        key = f"{path}.{name}" if path else name
        if field.description is None:
            yield key
        for t in _models(field.annotation):
            yield from _described(t, key)


def _models(annotation):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in typing.get_args(annotation):
        if arg is not types.NoneType:
            yield from _models(arg)


def test_every_undescribed_field_has_help():
    missing = []
    for key in _described(GateConfig):
        # models is a dict of roles: the role name is not a path segment
        norm = ".".join(p for p in key.split(".") if p not in DEFAULT_MODELS)
        if norm not in FIELD_HELP:
            missing.append(key)
    assert missing == []
    assert "permissions" in FIELD_HELP and "classifier" in FIELD_HELP["permissions"][1]


def test_role_gloss_covers_every_role():
    assert set(ROLE_GLOSS) == set(DEFAULT_MODELS)
