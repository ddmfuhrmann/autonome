"""The settings page's translation between form fields and the gate config: pure functions, no I/O.

Field names are flat (`gates-0-name`, `triage-reject_at`, `models-reviewer-effort`); lists of rows
(gates, schedules, smoke test checks) are ordered by their index. A checkbox is on when its key is
present with a truthy value. Text areas hold one item per line; a multi-select may send a list."""

import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from .gates import DEFAULT_MODELS, GateConfig

_TRUE = {"on", "true", "1", "yes", "sim"}
_UNITS = {"k": 1_000, "m": 1_000_000}
_ROW_INDEX = re.compile(r"^(gates|schedules|smoke_test-checks)-(\d+)(-.*)?$")

_GATE_FIELDS = ("name", "run", "mode", "fast", "when", "timeout_s")
_SCHEDULE_FIELDS = ("name", "kind", "every_hours", "run", "task")
_CHECK_FIELDS = ("name", "run", "timeout_s")
_SMOKE_FIELDS = ("up", "down", "url", "seed", "tester", "max_fixes")
_BUDGET_FIELDS = ("task_tokens", "task_minutes", "plan_tokens")


def _on(value: bool) -> str:
    return "on" if value else ""


def _lines(items: list[str]) -> str:
    return "\n".join(items)


def config_to_form(cfg: GateConfig) -> dict[str, str]:
    form: dict[str, str] = {}
    for i, g in enumerate(cfg.gates):
        form |= {f"gates-{i}-name": g.name, f"gates-{i}-run": g.run, f"gates-{i}-mode": g.mode,
                 f"gates-{i}-fast": _on(g.fast), f"gates-{i}-when": _lines(g.when),
                 f"gates-{i}-timeout_s": str(g.timeout_s)}
    form |= {"retries-max": str(cfg.retries.max), "retries-stop_on_repeat": _on(cfg.retries.stop_on_repeat)}
    form |= {f"budget-{f}": str(getattr(cfg.budget, f)) for f in _BUDGET_FIELDS}
    form["allowed_commands"] = _lines(cfg.allowed_commands)
    form["interactive"] = _on(cfg.interactive)
    form["permissions"] = cfg.permissions
    form["hold_before_merge-features"] = _lines(cfg.hold_before_merge.features)
    form["review_after_merge-features"] = _lines(cfg.review_after_merge.features)
    form |= {"review-enabled": _on(cfg.review.enabled), "review-max_rounds": str(cfg.review.max_rounds)}
    t = cfg.triage
    form |= {"triage-model": t.model if t else "off", "triage-fallback": t.fallback if t else "haiku",
             "triage-reject_at": str(t.reject_at if t else 0.7), "triage-max_rounds": str(t.max_rounds if t else 2)}
    s = cfg.smoke_test
    form |= {"smoke_test-up": s.up if s else "", "smoke_test-down": s.down if s else "",
             "smoke_test-url": s.url if s else "", "smoke_test-seed": (s.seed or "") if s else "",
             "smoke_test-tester": _on(s.tester if s else True), "smoke_test-max_fixes": str(s.max_fixes if s else 2)}
    for i, c in enumerate(s.checks if s else []):
        form |= {f"smoke_test-checks-{i}-name": c.name, f"smoke_test-checks-{i}-run": c.run,
                 f"smoke_test-checks-{i}-timeout_s": str(c.timeout_s)}
    for i, sc in enumerate(cfg.schedules):
        form |= {f"schedules-{i}-name": sc.name, f"schedules-{i}-kind": sc.kind,
                 f"schedules-{i}-every_hours": str(sc.every_hours), f"schedules-{i}-run": sc.run or "",
                 f"schedules-{i}-task": sc.task or ""}
    for role, m in cfg.models.items():
        form |= {f"models-{role}-model": m.model, f"models-{role}-effort": m.effort,
                 f"models-{role}-fallback": m.fallback or ""}
    form["retro_after_wave"] = _on(cfg.retro_after_wave)
    form["language"] = cfg.language
    return form


def _scalar(form: Mapping[str, str | list[str]], key: str) -> str:
    v = form.get(key, "")
    if isinstance(v, list):
        v = next((x for x in v if x.strip()), "")
    return v.strip()


def _items(form: Mapping[str, str | list[str]], key: str, commas: bool = True) -> list[str]:
    v = form.get(key, "")
    if isinstance(v, list):
        parts = v
    else:
        parts = re.split(r"[\n,]" if commas else r"\n", v)
    return [p.strip() for p in parts if p.strip()]


def _checked(form: Mapping[str, str | list[str]], key: str) -> bool:
    return _scalar(form, key).lower() in _TRUE


def _indexes(form: Mapping[str, Any], prefix: str) -> list[int]:
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)-")
    return sorted({int(m.group(1)) for k in form if (m := pattern.match(k))})


def _number(text: str, field: str, errors: dict[str, str], *, kind: type = int, units: bool = False):
    """The number in `text`, or None (empty, or an error recorded on `field`)."""
    if not text:
        return None
    try:
        if kind is float:
            return float(text.replace(",", "."))
        if units and text[-1].lower() in _UNITS:
            return int(float(text[:-1].replace(",", ".")) * _UNITS[text[-1].lower()])
        return int(text)
    except (ValueError, OverflowError):  # OverflowError: int(inf) from `infk` or `1e999k`
        errors[field] = ("Número, como 0.7." if kind is float else
                         "Número inválido. Use 400000, 400k ou 3M." if units else "Número inteiro.")
        return None


def _int_fields(form, prefix: str, fields: tuple[str, ...], errors, *, units: bool = False) -> dict[str, int]:
    out = {}
    for f in fields:
        n = _number(_scalar(form, f"{prefix}-{f}"), f"{prefix}-{f}", errors, units=units)
        if n is not None:
            out[f] = n
    return out


def _rows(form, prefix: str, fields: tuple[str, ...], blank_when: tuple[str, ...]) -> list[tuple[int, dict[str, str]]]:
    """(form index, raw values) of each row that is not blank; a row is blank when its `blank_when` fields are empty."""
    rows = []
    for i in _indexes(form, prefix):
        raw = {f: _scalar(form, f"{prefix}-{i}-{f}") for f in fields if f not in ("fast", "when")}
        raw["fast"] = _checked(form, f"{prefix}-{i}-fast")
        raw["when"] = _items(form, f"{prefix}-{i}-when", commas=False)
        if any(raw[f] for f in blank_when):
            rows.append((i, raw))
    return rows


def _build(form: Mapping[str, str | list[str]], errors: dict[str, str]) -> tuple[dict, dict[str, dict[int, int]]]:
    """The raw config dict (with its defaults filled in by the schema) and, per row list, cfg index → form index."""
    index_map: dict[str, dict[int, int]] = {}
    data: dict[str, Any] = {}

    gates = []
    for n, (i, raw) in enumerate(_rows(form, "gates", _GATE_FIELDS, ("name", "run", "when"))):
        index_map.setdefault("gates", {})[n] = i
        g: dict[str, Any] = {"name": raw["name"], "run": raw["run"], "fast": raw["fast"], "when": raw["when"]}
        if raw["mode"]:
            g["mode"] = raw["mode"]
        if (t := _number(raw["timeout_s"], f"gates-{i}-timeout_s", errors)) is not None:
            g["timeout_s"] = t
        gates.append(g)
    data["gates"] = gates

    retries = _int_fields(form, "retries", ("max",), errors)
    retries["stop_on_repeat"] = _checked(form, "retries-stop_on_repeat")
    data["retries"] = retries
    data["budget"] = _int_fields(form, "budget", _BUDGET_FIELDS, errors, units=True)
    data["allowed_commands"] = _items(form, "allowed_commands")
    data["interactive"] = _checked(form, "interactive")
    if perm := _scalar(form, "permissions"):
        data["permissions"] = perm
    data["hold_before_merge"] = {"features": _items(form, "hold_before_merge-features")}
    data["review_after_merge"] = {"features": _items(form, "review_after_merge-features")}
    data["review"] = {"enabled": _checked(form, "review-enabled"),
                      **_int_fields(form, "review", ("max_rounds",), errors)}

    if (model := _scalar(form, "triage-model")) and model != "off":
        triage: dict[str, Any] = {"model": model}
        if fb := _scalar(form, "triage-fallback"):
            triage["fallback"] = fb
        if (r := _number(_scalar(form, "triage-reject_at"), "triage-reject_at", errors, kind=float)) is not None:
            triage["reject_at"] = r
        triage |= _int_fields(form, "triage", ("max_rounds",), errors)
        data["triage"] = triage

    smoke = {f: v for f in ("up", "down", "url", "seed") if (v := _scalar(form, f"smoke_test-{f}"))}
    if any(f in smoke for f in ("up", "down", "url")):
        smoke["tester"] = _checked(form, "smoke_test-tester")
        smoke |= _int_fields(form, "smoke_test", ("max_fixes",), errors)
        checks = []
        for n, (i, raw) in enumerate(_rows(form, "smoke_test-checks", _CHECK_FIELDS, ("name", "run"))):
            index_map.setdefault("smoke_test-checks", {})[n] = i
            c: dict[str, Any] = {"name": raw["name"], "run": raw["run"]}
            if (t := _number(raw["timeout_s"], f"smoke_test-checks-{i}-timeout_s", errors)) is not None:
                c["timeout_s"] = t
            checks.append(c)
        smoke["checks"] = checks
        data["smoke_test"] = smoke

    schedules = []
    for n, (i, raw) in enumerate(_rows(form, "schedules", _SCHEDULE_FIELDS, ("name", "run", "task"))):
        index_map.setdefault("schedules", {})[n] = i
        s: dict[str, Any] = {"name": raw["name"], "kind": raw["kind"], "run": raw["run"] or None,
                             "task": raw["task"] or None}
        if (h := _number(raw["every_hours"], f"schedules-{i}-every_hours", errors)) is not None:
            s["every_hours"] = h
        schedules.append(s)
    data["schedules"] = schedules

    models = {}
    for role, default in DEFAULT_MODELS.items():
        m = {"model": _scalar(form, f"models-{role}-model") or default.model,
             "effort": _scalar(form, f"models-{role}-effort") or default.effort}
        if fb := _scalar(form, f"models-{role}-fallback"):
            m["fallback"] = fb
        models[role] = m
    data["models"] = models

    data["retro_after_wave"] = _checked(form, "retro_after_wave")
    data["language"] = _scalar(form, "language")
    return data, index_map


def _field_name(loc: tuple) -> str:
    parts = [str(p) for p in loc]
    while len(parts) > 1 and parts[-1].isdigit() and not parts[-2].isdigit() and parts[-2] not in (
            "gates", "schedules", "checks"):
        parts.pop()  # an item of a plain list belongs to the list's field
    return "-".join(parts)


def _remap(field: str, index_map: dict[str, dict[int, int]]) -> str:
    m = _ROW_INDEX.match(field)
    if not m:
        return field
    prefix, n, rest = m.group(1), int(m.group(2)), m.group(3) or ""
    return f"{prefix}-{index_map.get(prefix, {}).get(n, n)}{rest}"


def _prune(cfg: GateConfig) -> dict:
    """The config without anything that equals its default (models: only the roles that differ)."""
    out = cfg.model_dump(mode="json", exclude_defaults=True, exclude={"models"})
    if models := {r: m.model_dump(mode="json", exclude_defaults=True)
                  for r, m in cfg.models.items() if m != DEFAULT_MODELS.get(r)}:
        out["models"] = models
    return out


def form_to_config(form: Mapping[str, str | list[str]], catalog: set[str]) -> tuple[dict, dict[str, str]]:
    """(config without defaults, errors by field name). The config is empty when the schema rejects the form."""
    errors: dict[str, str] = {}
    data, index_map = _build(form, errors)
    try:
        cfg = GateConfig.model_validate(data)
    except ValidationError as e:
        for err in e.errors():
            field = _remap(_field_name(err["loc"]), index_map)
            errors.setdefault(field, "Obrigatório." if err["type"] == "missing" else err["msg"])
        return {}, errors
    for field, message in extra_checks(cfg, catalog).items():
        errors.setdefault(_remap(field, index_map), message)
    return _prune(cfg), errors


def _bad_name(name: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in name)


def extra_checks(cfg: GateConfig, catalog: set[str]) -> dict[str, str]:
    """What the schema accepts but the page must not save, by field name (indexes are the config's)."""
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for i, g in enumerate(cfg.gates):
        if not g.run.strip():
            errors[f"gates-{i}-run"] = "Obrigatório. Um gate precisa de um comando."
        if not g.name.strip():
            errors[f"gates-{i}-name"] = "Obrigatório. Dê um nome ao gate."
        elif _bad_name(g.name):
            errors[f"gates-{i}-name"] = "O nome tem um caractere de controle."
        elif g.name in seen:
            errors[f"gates-{i}-name"] = f"Nome repetido: {g.name}."
        seen.add(g.name)
    for i, s in enumerate(cfg.schedules):
        if s.kind == "command" and not (s.run or "").strip():
            errors[f"schedules-{i}-run"] = "Obrigatório. Um schedule do tipo command precisa de um comando."
    for section, features in (("hold_before_merge", cfg.hold_before_merge.features),
                              ("review_after_merge", cfg.review_after_merge.features)):
        if unknown := [f for f in features if f not in catalog]:
            errors[f"{section}-features"] = f"Feature fora do catálogo: {', '.join(unknown)}."
    for f in _BUDGET_FIELDS:
        if getattr(cfg.budget, f) <= 0:
            errors[f"budget-{f}"] = "Precisa ser maior que zero."
    return errors


def _flatten(value: Any, path: str = "") -> dict[str, Any]:
    """Leaves of a dumped config by dotted path; None counts as absent, lists are leaves."""
    if isinstance(value, dict):
        return {k: v for key, item in value.items() for k, v in _flatten(item, f"{path}.{key}" if path else key).items()}
    return {} if value is None else {path: value}


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "sim" if v else "não"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "vazio"
    return str(v) if str(v) != "" else "vazio"


def _diff(label: str, old: dict[str, Any], new: dict[str, Any], name_of=lambda p: p, sep: str = ": ") -> list[str]:
    out = []
    for path in dict.fromkeys([*old, *new]):
        o, n = old.get(path), new.get(path)
        head = f"{label}{name_of(path)}{sep}"
        if path in old and path in new:
            if o != n:
                out.append(f"{head}{_fmt(o)} → {_fmt(n)}")
        elif path in new:
            out.append(f"{head}{_fmt(n)} (novo)")
        else:
            out.append(f"{head}{_fmt(o)} → removido")
    return out


def _item_changes(kind: str, old: list[dict], new: list[dict]) -> list[str]:
    old_by, new_by = {x["name"]: x for x in old}, {x["name"]: x for x in new}
    out = []
    for name, item in new_by.items():
        if name not in old_by:
            out.append(f"{kind} {name} adicionado")
        else:
            out += _diff(f"{kind} {name}: ", _flatten(old_by[name]), _flatten(item),
                         lambda p: "timeout" if p == "timeout_s" else p, sep=" ")
    out += [f"{kind} {name} removido" for name in old_by if name not in new_by]
    kept_old = [n for n in old_by if n in new_by]
    if kept_old != [n for n in new_by if n in old_by]:
        out.append(f"Ordem de {kind.lower()}s mudou")
    return out


def _label(path: str) -> str:
    _, role, field = path.split(".")
    return f"Modelo {role}: {field}"


def field_changes(old: GateConfig | None, new: GateConfig) -> list[str]:
    """What changed, in words, field by field. No old config means the defaults."""
    o, n = (old or GateConfig()).model_dump(mode="json"), new.model_dump(mode="json")
    out = _item_changes("Gate", o.pop("gates"), n.pop("gates"))
    out += _item_changes("Schedule", o.pop("schedules"), n.pop("schedules"))
    o_checks = (o.get("smoke_test") or {}).pop("checks", [])
    n_checks = (n.get("smoke_test") or {}).pop("checks", [])
    out += _diff("", _flatten(o), _flatten(n), lambda p: _label(p) if p.startswith("models.") else p)
    out += _item_changes("Check", o_checks, n_checks)
    return out
