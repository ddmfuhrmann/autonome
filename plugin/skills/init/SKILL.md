---
name: init
description: Prepare a project for autonome before its first plan - read the repo, check the machine, go through each capability with the user, then propose the settings (the user reviews and saves them in the Settings), the feature catalog and the first conventions. Use when the project has no gate config yet, when the user wants to set up or revisit the project's settings, or invokes /autonome:init.
---

# init: prepare the project

You read the repo and the memory, check the machine, ask the user only what the repo does not
answer, and hand over a **proposal**. The user reviews and saves the settings in the Settings page;
you never save them.

Hard rules:

- **Never write a file in the repo** (CI, scripts, `.gitignore`) **and never save the gate config.**
  The settings go through `settings_propose`; only the human saves them.
- **Never install anything** (Docker, `gh`, the Jev key). Check and say what is missing.
- Do not depend on a dialog (MCP elicitation). Everything is chat plus the review link.
- Write for the user in the gate config's `language` when there is one. UI text stays in English.

## 1. Read

- **The repo**: stack markers (`pyproject.toml`, `uv.lock`, `package.json` and its lockfile, `pom.xml`,
  `go.mod`, `Cargo.toml`), the scripts (`package.json` scripts, `pyproject.toml` tools, `Makefile`
  targets), CI files (`.github/workflows`), `compose.yml`, and the code folders.
- **The memory**: `gate_config_get`, `features_get`, `conventions_get` for the project slug.
- **A saved config already exists?** Then propose only what changes, and say so up front: what you
  keep, what you would change and why. Do not redo the whole interview.

## 2. The machine

Run `${CLAUDE_PLUGIN_ROOT}/bin/autonome doctor --project <slug>` and tell the user what is missing
(context, Docker, the Jev key, `gh`). Do not install or fix anything.

## 3. Triage

One question per message, with your recommendation first. Ask only what the repo does not answer.

- **Gates**: built from commands the repo already runs (scripts, Makefile, CI), never invented names.
  Cheap ones (format, lint, types) are `fast`; tests and builds are not. Use the project's own runner
  (`uv run pytest`, `pnpm test`, `./mvnw`), and put every command's binary in `allowed_commands`.
- `language` for what agents write for people.
- `permissions` (`dontAsk` allowlist, or `auto`).
- `triage`: only if the Jev key exists on the machine.
- `smoke_test`: only if there is a `compose.yml`.
- `merge` (direct or PR): only if `gh` is installed and the remote is on GitHub.
- `task_size`, `hold_before_merge` and review after merge, `schedules`.
- `models`: leave the defaults unless the user asks.

## 4. Catalog

Propose 4 to 10 features from the repo's folders: slug, one sentence (a stable capability of the
product, not a change), and path globs. Confirm the list with the user, then create each one with
`feature_add`. A `similar_to` answer means it already exists: do not force it.

## 5. Conventions

Propose 2 to 4 verifiable rules per layer the repo has, with `convention_propose` (title, rule, why,
layers). Each one must be checkable by a gate or a reviewer, and drawn from what the repo already
does (its lint config, test layout, naming). The human accepts them in the UI.

## 6. Hand over

The YAML format is the gate config's, described in `docs/configuration.md` of the autonome repo (read
it, or use the saved config from `gate_config_get` as the example); use only the keys documented
there. Minimal skeleton: `gates` (each with `name`, `run`, `mode`, `fast`, `when`, `timeout_s`),
`allowed_commands`, `retries`, `budget`, `hold_before_merge`, `schedules`; the optional blocks
(`language`, `permissions`, `triage`, `smoke_test`, `merge`, `task_size`, `models`) only when decided above.

Call `settings_propose` with the full YAML and a `note` (at most 500 characters) of what was decided.
Give the user the `review_at` link and say that only they save it, in the Settings. Then suggest
`/autonome:plan` for the first feature.
