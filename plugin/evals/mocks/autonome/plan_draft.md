---
type: agent
---

You play the autonome context server's `plan_draft` tool, which validates and saves a draft plan.

If `project` is not a string, or `plan` is not an object with a string `title`, a string `goal` and a
`tasks` array, reply with only {"error": "invalid plan: <what is wrong>"} — the real server rejects it
the same way.

Otherwise reply with only this JSON object, no prose:

{"plan_id": <id>, "status": "draft", "approve_at": "http://127.0.0.1:8765/ui/p/<project>/documents/<id>"}

<project> is the `project` argument. <id> is the `plan_id` argument when one is given (a revision of
that draft). Otherwise it is 100 plus the number of characters in the `feature` argument (42 when no
feature is given), so different features get different ids.
