"""Stack presets: the gates and commands a project of a given stack usually starts with. Plain data in
the shape of `Gate`; the first word of each `run` is the stack's own tool, because it becomes the
implementer's permission."""

import copy
from dataclasses import dataclass

from autonome_context.gates import GateConfig


@dataclass(frozen=True)
class Preset:
    label: str
    gates: list[dict]
    allowed_commands: list[str]


PRESETS: dict[str, Preset] = {
    "python": Preset(
        label="Python",
        gates=[
            {"name": "ruff-format", "run": "uv run ruff format .", "mode": "fix", "fast": True},
            {"name": "ruff-check", "run": "uv run ruff check .", "fast": True},
            {"name": "mypy", "run": "uv run mypy src", "fast": True},
            {"name": "pytest", "run": "uv run pytest -q"},
        ],
        allowed_commands=["uv"],
    ),
    "typescript": Preset(
        label="TypeScript",
        gates=[
            {"name": "eslint", "run": "pnpm exec eslint --fix .", "mode": "fix", "fast": True},
            {"name": "tsc", "run": "pnpm exec tsc --noEmit", "fast": True},
            {"name": "vitest", "run": "pnpm exec vitest run"},
        ],
        allowed_commands=["pnpm", "node"],
    ),
    "java": Preset(
        label="Java",
        gates=[
            {"name": "spotless", "run": "./mvnw -q spotless:apply", "mode": "fix", "fast": True},
            {"name": "checkstyle", "run": "./mvnw -q checkstyle:check", "fast": True},
            {"name": "maven-verify", "run": "./mvnw -q verify"},
        ],
        allowed_commands=["./mvnw"],
    ),
    "go": Preset(
        label="Go",
        gates=[
            {"name": "gofmt", "run": "gofmt -w .", "mode": "fix", "fast": True},
            {"name": "go-vet", "run": "go vet ./...", "fast": True},
            {"name": "go-test", "run": "go test ./..."},
        ],
        allowed_commands=["go"],
    ),
}


def preset_config(name: str, keep: set[str]) -> GateConfig:
    """The preset with only the gates named in `keep`; everything else at its default. Raises
    KeyError for an unknown preset."""
    preset = PRESETS[name]
    return GateConfig.model_validate({
        "gates": copy.deepcopy([g for g in preset.gates if g["name"] in keep]),
        "allowed_commands": list(preset.allowed_commands),
    })
