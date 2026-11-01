# Briefing: task #478 of project autonome

## Task: The triage module: questions, backends, verdict
Layers: integration, application, test

One module holds the questions, calls Jev or Haiku, and decides in code.

**Where**
- `plugin/runner/src/triage.ts` (new), `plugin/runner/src/triage.test.ts` (new)
- `plugin/runner/src/context.ts`: `Triage` type in the gate config

**How**
- Questions (nouls, each one judgment, phrased so true means yes, referring to `deviation` in the state):
  - `drops_coverage`: removes or weakens a test, check or gate instead of fixing what it found
  - `contradicts`: goes against the task, the plan's out-of-scope or a convention in the state
  - `workaround`: silences or works around a problem (suppression, stub, ignore, allow) instead of solving it
  - `required_by_plan`: the task or plan in the state asks for exactly what was done
  - `narrower`: keeps the change smaller than the task, with the same result
- `verdict(answers, rejectAt)`: workaround counts only when `required_by_plan < rejectAt`; reject if any of drops_coverage, contradicts, workaround ≥ rejectAt; else accept. `narrower` is recorded, not used.
- `jev(state, key)`: `fetch` to the Jev endpoint (URL overridable by `TYPESAFE_URL`), all questions in one request, 30 s timeout; non-2xx, timeout or a missing answer throws. The key is a parameter, never read inside the module.
- `haiku(state)`: `consult` with role model haiku/low and a JSON schema `{answers: {name: number 0..1}}`, asking for each statement the probability it is true; out-of-range or missing values throw.
- `judge(state, cfg, backends)`: tries `cfg.model`, then `cfg.fallback`; returns `{backend, model, answers, verdict}` or throws when both fail or both are off. Backends are injected so tests use fakes.

## Plan: Triage: a classifier judges deviations, the human aligns by rules
Goal: Deviations are judged by a classifier (Jev, with a Haiku fallback) instead of waiting for the human.

### Jev (TypeSafe System One)
- `POST https://api.typesafe.ai/v1/systemone`, `Authorization: Bearer $TYPESAFE_API_KEY`, body `{model, state, questions}`; `state` may be a JSON object; each question `{type: "noul", instructions}` answers `{noul: 0..1}`; response has `usage`.
- TypeSafe's guidance: Jev makes atomic judgments, code makes the decision; a failed call is an error, never an answer.

## Conventions for these layers
### External model calls fail as errors
A failed, timed-out or malformed call to Jev or Haiku is raised and logged as an error; it never becomes an answer.
### Tests fake external models
Tests reach Jev and the Agent SDK only through an injected fake; no test needs TYPESAFE_API_KEY or network.
