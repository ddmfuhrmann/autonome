---
description: The project already has a saved config (Java gates). The init should say so and propose only what changes, not redo the whole interview or the existing gates.
tags: [init, gates]
max_turns: 30
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

/autonome:init for project `shop`. It already has a saved config. The repo now also has a new
`admin/` folder (Vite + React, pnpm, with a `test` script and a `build` script in its
`package.json`). Everything else stays as it is. I decided everything, don't ask me anything, just
propose what changes.
