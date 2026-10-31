---
expect:
  project: string
  wave_id: number
---

{"status": "draft", "human": "declined", "approve_at": "http://127.0.0.1:8765/ui/p/{{input.project}}/documents/{{input.wave_id}}", "note": "Nothing was approved. The draft waits; the human can approve it in the UI."}
