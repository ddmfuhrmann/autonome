---
description: The request leaves an approach open (how the ledger reaches the database). The composer should record it in the plan's design with the options weighed, follow it in the tasks, and turn it into a convention that outlives the plan.
tags: [compose, design]
max_turns: 25
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_AUTONOME_OFFLINE: "1"
---

Project `shop`. Add a ledger: every order payment and refund becomes an entry (amount, currency,
order id, kind), and `GET /ledger?order_id=` lists them. Persistence is Postgres through JPA. Use a
repository per aggregate for the data access. I have decided everything I care about, don't ask me
anything, just draft the plan.
