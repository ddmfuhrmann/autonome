import pytest

from autonome_context import settings_form
from autonome_context.gates import GateConfig
from autonome_context.presets import PRESETS, preset_config


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_preset_is_valid(name):
    preset = PRESETS[name]
    cfg = GateConfig.model_validate({"gates": preset.gates, "allowed_commands": preset.allowed_commands})
    assert settings_form.extra_checks(cfg, set()) == {}
    names = [g["name"] for g in preset.gates]
    assert all(n.strip() for n in names)
    assert len(names) == len(set(names))


def test_preset_config_keeps_only_named_gates():
    cfg = preset_config("go", {"go-test"})
    assert [g.name for g in cfg.gates] == ["go-test"]
    assert cfg.allowed_commands == ["go"]


def test_unknown_preset_raises():
    with pytest.raises(KeyError):
        preset_config("cobol", set())


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_no_shell_or_python_runs(name):
    for g in PRESETS[name].gates:
        assert g["run"].split()[0] not in {"python", "sh", "bash"}
