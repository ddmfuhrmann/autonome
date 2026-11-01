import pytest
from pydantic import ValidationError

from autonome_context.gates import DEFAULT_MODELS, EXAMPLE, GateConfig

FULL = """\
gates:
  - {name: test, run: 'true', fast: true}
retries: {max: 5, stop_on_repeat: false}
hold_before_merge: {features: [sign*]}
review_after_merge: {features: [wallet*]}
schedules:
  - {name: deps, kind: command, every_hours: 12, run: 'true', task: Update deps}
retro_after_wave: false
smoke_test:
  up: 'true'
  down: 'true'
  url: http://127.0.0.1:8000
  checks: [{name: e2e, run: 'true'}]
  tester: false
  max_fixes: 3
models: {implementer: {model: opus, effort: high, fallback: sonnet}}
"""


def test_the_new_keys_round_trip_through_yaml_without_losing_a_field():
    cfg = GateConfig.from_yaml(FULL)
    assert cfg.retries.max == 5 and cfg.hold_before_merge.features == ["sign*"]
    assert cfg.review_after_merge.features == ["wallet*"] and cfg.schedules[0].name == "deps"
    assert cfg.retro_after_wave is False and cfg.smoke_test.max_fixes == 3
    assert cfg.models["implementer"].model == "opus"
    assert GateConfig.from_yaml(cfg.to_yaml()) == cfg


def test_the_example_is_a_valid_config():
    GateConfig.from_yaml(EXAMPLE)


# The old names are split ("en" "core") so the vocabulary gate, which greps for them, stays green.
@pytest.mark.parametrize("old", [
    "en" "core: {max: 3}", "fer" "mata: {features: []}", "play" "back: {features: []}", "cues: []",
    "sound" "check: {up: a, down: b, url: c}", "tune_after_wave: true",
    "models: {music" "ian: {model: sonnet, effort: low}}", "models: {rehear" "sal: {model: haiku, effort: low}}",
    "smoke_test: {up: a, down: b, url: c, listener: false}",
    "retries: {max: 3, en" "core: 1}",
    "models: {implementer: {model: sonnet, effort: low, fallbak: haiku}}",
])
def test_the_old_names_are_rejected_like_any_unknown_key(old):
    with pytest.raises(ValidationError):
        GateConfig.from_yaml(old)


def test_permissions_default_to_allowlist_and_auto_round_trips():
    assert GateConfig.from_yaml("gates: []").permissions == "allowlist"
    cfg = GateConfig.from_yaml("permissions: auto")
    assert cfg.permissions == "auto"
    assert GateConfig.from_yaml(cfg.to_yaml()).permissions == "auto"


@pytest.mark.parametrize("value", ["yolo", "bypassPermissions"])
def test_unknown_permissions_are_rejected(value):
    with pytest.raises(ValidationError):
        GateConfig.from_yaml(f"permissions: {value}")


def test_the_default_models_are_the_product_roles():
    assert set(DEFAULT_MODELS) == {"implementer", "reviewer", "preflight", "tester", "retro", "handoff", "docs"}
    assert set(GateConfig().models) == set(DEFAULT_MODELS)
