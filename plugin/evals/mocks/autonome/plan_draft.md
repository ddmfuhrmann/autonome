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
that draft). Otherwise look the `feature` argument up in this table, exactly:

| feature | id |
|---|---|
| wishlist | 201 |
| wishlist-alerts | 202 |
| admin-dashboard | 203 |
| ledger | 204 |
| balance | 205 |
| welcome-email | 206 |
| (missing, or any other) | 209 |

Different features must get different ids, so never answer 209 for a feature in the table.
