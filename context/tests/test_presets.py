import copy

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


def test_preset_config_shares_no_state_with_presets(monkeypatch):
    before = copy.deepcopy(PRESETS["go"])
    cfg = preset_config("go", {"go-test", "go-vet"})
    cfg.gates[0].run = "rm -rf /"
    cfg.gates.pop()
    cfg.allowed_commands.append("rm")
    assert PRESETS["go"] == before

    # the gates handed to validation are copies, not the preset's own dicts
    seen = []
    original = GateConfig.model_validate
    monkeypatch.setattr(
        GateConfig, "model_validate", classmethod(lambda cls, data: seen.append(data) or original(data))
    )
    preset_config("go", {"go-test"})
    assert all(g is not p for g in seen[0]["gates"] for p in PRESETS["go"].gates)
    assert seen[0]["allowed_commands"] is not PRESETS["go"].allowed_commands


def test_unknown_preset_raises():
    with pytest.raises(KeyError):
        preset_config("cobol", set())


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_no_shell_or_python_runs(name):
    for g in PRESETS[name].gates:
        assert g["run"].split()[0] not in {"python", "sh", "bash"}
