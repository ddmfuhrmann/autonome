---
description: The request invites over-engineering and leaves scope implied. The composer should challenge its own plan before saving it - name a simpler path and the hidden scope - without asking a round of questions.
tags: [compose, challenge]
max_turns: 25
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

Project `shop`. When a new account is created, send a welcome email. Set up an event bus (Kafka) for
it, so other features can subscribe to account events later. There is already an EmailSender port.
I have decided everything I care about, don't ask me anything, just draft the plan.
