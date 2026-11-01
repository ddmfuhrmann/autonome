---
description: The backlog item needs a PRD and none exists. The planner should start the product spec with the user, in the user's language, before any plan.
tags: [plan, prd]
max_turns: 15
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

Projeto `shop`. Vamos planejar o item w2-refunds do backlog: devolver créditos pra carteira do
cliente quando uma consulta paga falha no bureau.
