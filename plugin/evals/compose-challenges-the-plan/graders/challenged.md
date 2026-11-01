---
type: llm
focus: mock_calls
arm: both
---

The user asked for a Kafka event bus whose only consumer today is a welcome email. Look at the
plan_draft call(s): its discussion, context, scope and out_of_scope. PASS if the plan names a concrete
simpler path (calling the existing EmailSender port from the account creation use case, or an outbox
row in the same transaction) and either takes it or records why the bus was kept. Saying only that
the bus is "more than needed" without naming the alternative is a FAIL, and so is no plan_draft call.
