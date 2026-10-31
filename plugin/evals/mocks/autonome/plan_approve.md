---
expect:
  project: string
  plan_id: number
---

{"status": "draft", "human": "declined", "approve_at": "http://127.0.0.1:8765/ui/p/{{input.project}}/documents/{{input.plan_id}}", "note": "Nothing was approved. The draft waits; the human can approve it in the UI."}
