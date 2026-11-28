# Attempt-scoped landing receipts

A trusted landing callback is idempotent on `(work_item_id, attempt_id)`.
WorkItem IDs are immutable globally unique resource IDs and bind the receipt
to the WorkItem's immutable owner/tenant boundary. This v0.1 model has no
Assignment generation; a deliberate relink therefore requires a new Attempt
ID, not a second landing callback for an old Attempt.

`authority_landing_events` has a partial unique index over the WorkItem and
Attempt identity. An exact duplicate callback returns the existing event ID.
A callback that reuses the Attempt with a different session, match outcome, or
evidence-completeness value is rejected; the original receipt is not rewritten.

`link_session` creates the SessionLink and consumes the landing receipt in one
SQLite transaction. The receipt stores the resulting `link_id` alongside
`consumed_at`. Unlinking a SessionLink never clears or reopens that receipt, so
retrying landing ingestion or `session.link` cannot resurrect the same Attempt.
A new Attempt can create a new receipt and a deliberate subsequent link.

On initialization, the schema migration backfills historical `link_id` values
only when the old `(session_id, work_item_id, consumed_at)` relationship maps
to exactly one SessionLink. Ambiguous mappings or duplicate legacy Attempt
rows fail closed and preserve all source rows; they require explicit manual
reconciliation. No legacy event or SessionLink is automatically deleted.

This is a repository-only change. It does not authorize production database
migration, deployment, or mutation.
