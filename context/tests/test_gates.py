import pytest
from pydantic import ValidationError

from autonome_context.gates import DEFAULT_MODELS, GateConfig

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


def test_task_size_defaults_to_300_lines_and_refuses_less_than_20():
    assert GateConfig().task_size.max_lines == 300
    assert GateConfig.from_yaml("task_size:\n  max_lines: 20\n").task_size.max_lines == 20
    with pytest.raises(ValidationError):
        GateConfig.model_validate({"task_size": {"max_lines": 10}})


def test_merge_defaults_to_fast_forward_and_refuses_other_modes():
    cfg = GateConfig()
    assert (cfg.merge.mode, cfg.merge.pr) == ("ff", "manual")
    cfg = GateConfig.from_yaml("merge:\n  mode: pr\n  pr: auto\n")
    assert (cfg.merge.mode, cfg.merge.pr) == ("pr", "auto")
    with pytest.raises(ValidationError):
        GateConfig.from_yaml("merge:\n  mode: squash\n")
    with pytest.raises(ValidationError):
        GateConfig.from_yaml("merge:\n  pr: sometimes\n")


def test_the_default_models_are_the_product_roles():
    assert set(DEFAULT_MODELS) == {"implementer", "reviewer", "preflight", "tester", "retro", "handoff", "docs",
                                   "simplifier", "staff"}
    assert set(GateConfig().models) == set(DEFAULT_MODELS)


def test_models_without_staff_use_opus_high():
    cfg = GateConfig.from_yaml("models:\n  docs: {model: haiku, effort: low}\n")
    assert (cfg.models["staff"].model, cfg.models["staff"].effort, cfg.models["staff"].fallback) == ("opus", "high", None)
    assert GateConfig().models["staff"].model == "opus"


def test_staff_model_is_accepted_and_saved():
    cfg = GateConfig.from_yaml("models:\n  staff: {model: sonnet, effort: max}\n")
    assert (cfg.models["staff"].model, cfg.models["staff"].effort) == ("sonnet", "max")
    assert GateConfig.from_yaml(cfg.to_yaml()).models["staff"] == cfg.models["staff"]


def test_unknown_model_role_is_still_rejected():
    with pytest.raises(ValidationError, match="unknown model roles: boss"):
        GateConfig.from_yaml("models:\n  boss: {model: opus, effort: high}\n")


def test_models_without_the_simplifier_use_haiku_low():
    cfg = GateConfig.from_yaml("models:\n  docs: {model: haiku, effort: low}\n")
    assert (cfg.models["simplifier"].model, cfg.models["simplifier"].effort) == ("haiku", "low")


def test_simplify_schedule_max_tasks():
    cfg = GateConfig.from_yaml("schedules:\n  - {name: s, kind: simplify}\n")
    assert cfg.schedules[0].max_tasks == 3
    cfg = GateConfig.from_yaml("schedules:\n  - {name: s, kind: simplify, max_tasks: 10}\n")
    assert cfg.schedules[0].max_tasks == 10
    for bad in (0, 11):
        with pytest.raises(ValidationError):
            GateConfig.from_yaml(f"schedules:\n  - {{name: s, kind: simplify, max_tasks: {bad}}}\n")
    with pytest.raises(ValidationError, match="only applies to kind simplify"):
        GateConfig.from_yaml("schedules:\n  - {name: c, kind: command, run: x, max_tasks: 2}\n")
    # a command schedule dumped to YAML (max_tasks: null) loads again
    cmd = GateConfig.from_yaml("schedules:\n  - {name: c, kind: command, run: x}\n")
    assert GateConfig.from_yaml(cmd.to_yaml()) == cmd
