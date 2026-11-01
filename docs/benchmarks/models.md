# Benchmark: model and effort per role

`autonome bench` runs cases with a known answer, taken from the shortlink v1 wave, on a grid of
model and effort. Scores are 0–1; cost is the SDK's estimate.

- **reviewer/post-links**: review shortlink's `POST /links` commit (`874c32c`) with three
  conventions in the briefing. 1.0 = flags control characters, the raw 500 and the bare dicts, and
  fails the review.
- **reviewer/raw-500**: review shortlink-v2's `POST /links and GET /{code}` commit (`56acffe`) with
  the task's real briefing. 1.0 = blocks on the raw 500 after exhausted code retries, which an
  active convention forbids (the Haiku reviewer of the v2 run rated it low and let it merge).
- **rehearsal/plan-16**: rehearse plan #16 against the repo after the quality-gates feature
  (`96cac2b`). 1.0 = verdict revise, naming the import contract the plan's spec cannot pass.

## 2026-10-31

| Case | Model:effort | Runs | Score | Cost per run | Time |
|---|---|---|---|---|---|
| reviewer/post-links | haiku:low | 4 | 1.00 every run | US$ 0.004–0.009 | 15–26 s |
| reviewer/post-links | sonnet:medium | 1 | 1.00 | US$ 0.055 | 13 s |
| reviewer/post-links | sonnet:high | 1 | 1.00 | US$ 0.098 | 22 s |
| rehearsal/plan-16 | haiku:low | 4 | 1.00 every run | US$ 0.007–0.016 | 26–56 s |
| rehearsal/plan-16 | sonnet:medium | 1 | 1.00 | US$ 0.201 | 27 s |
| rehearsal/plan-16 | sonnet:high | 1 | 1.00 | US$ 0.185 | 59 s |

Decision: Haiku with low effort is the default for reviewer and rehearsal, 6–20× cheaper with no
loss on these cases. Caveat: two cases, both with the answer spelled out in the briefing or the
gate rules. Add a harder case (a gap no convention names) before trusting it further; the
hardening wave on shortlink is the field test. Raise a role in the gate config's `models` if it
starts missing things.

Not measured yet: musician (measure on a real wave), listener, tune, handoff and docs scribes.

## 2026-10-31, after shortlink v2

v2 showed the reviewer finds what a convention forbids but may rate it low. Since `ba7a5de` a
"missing" finding against a named convention blocks whatever severity the model gave it.

| Case | Model:effort | Runs | Score | Cost per run | Time |
|---|---|---|---|---|---|
| reviewer/raw-500 | haiku:low | 2 | 1.00 every run | US$ 0.006 | 16–21 s |
| reviewer/raw-500 | sonnet:medium | 2 | 1.00 every run | US$ 0.09–0.18 | 12–17 s |

Haiku stays the reviewer's default: with the rule, severity no longer depends on the model's
judgement of how likely a case is.

## 2026-11-01, a gap no convention names

**reviewer/invented-api**: autonome's own T-478 (`76bc1f7`) wrote a Jev client for an endpoint and body
that do not exist (`POST /v1/nouls` with `{state, nouls}`), with tests faking that shape. The briefing
gives the plan's real API (`POST /v1/systemone` with `{model, state, questions}`). 1.0 = a high finding
on the mismatch and a failed review.

| Case | Model:effort | Runs | Score | Cost per run | Time |
|---|---|---|---|---|---|
| reviewer/invented-api | haiku:low | 2 | 1.00 every run | US$ 0.007 | 16–22 s |
| reviewer/invented-api | sonnet:medium | 2 | 1.00 every run | US$ 0.08–0.20 | 11–14 s |

In the run both the musician and the Haiku reviewer missed it, but not for lack of model: the task
briefing did not carry the plan's `context`, where the API shape was written. With the shape in hand
Haiku finds it every time. Since 0.11.0 the briefing carries the plan's context (capped at 3,000
characters). Haiku stays the reviewer's default; a project can still raise it in `models`.
