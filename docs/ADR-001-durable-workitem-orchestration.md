# ADR-001: Durable WorkItem Orchestration and Evidence-Gated Acceptance

- **Status:** Proposed design baseline; conceptual direction accepted by independent review, contract remains `ACCEPT_WITH_CHANGES`.
- **Implementation status:** BLOCKED / NOT AUTHORIZED.
- **Owner:** Session Ops (SO).
- **Canonical decision record:** [SO #166](https://github.com/oahcom/chatgpt-session-ops/issues/166).
- **Related contracts:** [SO #183](https://github.com/oahcom/chatgpt-session-ops/issues/183), [Brain #458](https://github.com/oahcom/AiMemoryBrainArchitecture/issues/458).

This ADR consolidates the durable orchestration contract. It is a documentation-only decision record; it does not authorize source changes, schema migration, tool-surface changes, deployment, or production data mutation.

## 1. Context and non-negotiable boundaries

The goal is a recoverable engineering workflow that can repeatedly decompose goals, dispatch bounded work, implement, independently review, repair, and accept changes. It is not an Issue-closing bot.

The control flow is:

`Orchestrator → Engineering Knowledge → WorkItem/Spec → Assignment → Attempt/Job → Evidence → Acceptance → next iteration`

The following boundaries are normative:

1. **SO is the only MCP-facing workflow/control plane.** SO owns WorkItems, Assignments, Attempts, Evidence, Acceptance, authorization, dispatch, and lifecycle transitions.
2. **Adapter is an independent project and optional context provider.** SO invokes Adapter Core through the local process contract. There is no Adapter MCP, second network hop, or Adapter-owned workflow authority. Adapter is not a Brain component or the implementation carrier for Brain's logical Agent Adapter boundary.
3. **Brain is a governance/reference architecture.** It does not override SO's actual consumer requirements.
4. **Memory is context, never authority.** Memory cannot establish identity, role, assignment, execution, test success, review, acceptance, or permission.
5. **Owner/Coordinator co-location is intentional.** One stable Orchestrator Session may hold Owner and Coordinator assignments across many WorkItems. Worker and Reviewer remain separate execution contexts.
6. **No second workflow service is introduced in v1.** Durable SO state plus Sender Job receipts and GitHub references must be sufficient to reconstruct progress after restart.

### Engineering Knowledge and proactive goal discovery

The Orchestrator continuously maintains a versioned, reviewable Engineering Knowledge artifact in the SO orchestration repository; v1 does not add a separate Knowledge service or mutable Knowledge database. The artifact contains:

- verified architecture map, cross-repository boundaries, invariants and explicit non-goals;
- claims tagged `verified`, `inferred`, or `unknown`, each with source references (repository commit + path/line, Issue/PR/CI, or dated production observation), last-verified time and evidence digest;
- decisions/ADRs with rationale and supersession links;
- unresolved questions, missing evidence, risk register and candidate architecture goals;
- current repository/branch/PR/CI state and dependency relationships.

The artifact is a derived, versioned knowledge index, not a replacement for source code, GitHub or production as the factual authority. The Orchestrator revalidates stale claims before dispatch and never upgrades a Worker assertion to `verified` without independent evidence. No separate Goal entity is introduced in v1: a validated candidate goal becomes a canonical GitHub Issue; multiple bounded WorkItems may implement that Issue. Unvalidated candidates remain in the artifact. After an Acceptance, the Orchestrator updates the artifact in a new commit; the next WorkItemSpec records the exact knowledge revision and source evidence used.

## 2. WorkItem identity and specification

A WorkItem is one bounded execution slice, not a mirror of a GitHub Issue.

- New WorkItems declare `workflow_version=1` and `kind=development` or `kind=operational`.
- A development WorkItem has exactly one immutable primary Issue reference: canonical `repository_full_name + issue_number`; the URL is derived. V1 allows the three canonical repositories: `oahcom/chatgpt-session-ops`, `oahcom/ai-memory-adapter`, and `oahcom/AiMemoryBrainArchitecture`.
- An operational WorkItem may omit a primary Issue. One Issue may own many bounded WorkItems. PRs, commits, CI runs, reviews, and decisions are typed evidence, not additional primary Issues.
- The primary Issue binding is immutable after the first Assignment or Attempt. Before activation it may be amended by an authorized Owner with an audit event; after activation, create a replacement WorkItem and preserve history.
- WorkItem creation is idempotent on a stable request ID scoped by authenticated tenant/principal and a canonical request digest. Same key + same payload returns the same WorkItem and initial Spec; same key + changed payload conflicts.

Each WorkItem has an immutable, versioned WorkItemSpec containing objective, in-scope boundaries, explicit non-goals, acceptance criteria, priority, hard dependencies, schema version, source-Issue provenance, and the Engineering Knowledge artifact commit/digest and claims used to derive the slice.

Dependencies form a validated DAG. Reject self-edges, duplicates, cross-tenant references, and cycles transactionally. A hard dependency is satisfied only by an Acceptance for the dependency's current Spec version, never by a status flag or Worker self-report. Material source/spec changes create a new Spec version and invalidate applicability of work/evidence based on the old version.

The source-Issue digest covers the exact normalized fields used for derivation (at minimum title, body, state, and relevant labels); observation time and `updated_at` are stored separately. Revalidate before dispatch, material re-specification, Acceptance, and closure. Unrelated metadata changes must not silently invalidate a Spec.

## 3. Assignment, roles, and Session reuse

Assignment is an immutable identity with append-only transition history. It binds tenant/principal, WorkItem, Spec version, role, Session reference, prompt/template version, exact rendered-input/context/transcript digests, generation, and role-specific code provenance.

Roles are `owner`, `coordinator`, `worker`, and `reviewer`. A role label is not an authorization grant.

### Role matrix and reuse

- **Owner/Coordinator:** one stable Orchestrator Session may hold Owner and/or Coordinator assignments across many WorkItems. These assignments do not create SessionLinks. Authority comes from the authenticated SO principal and explicit WorkItem-scoped capabilities. Coordinator has no implicit Owner privilege.
- **Worker:** a separate execution Session is bound to one WorkItem for its lifetime and is never rebound to another WorkItem. After Reviewer rejection, supersede the prior immutable Worker Assignment and create the next monotonic Assignment generation; the same Worker Session may continue within that WorkItem only if its SessionLink remains active and the full transcript/context digest is pinned, or a new Worker Session may be provisioned. Never create concurrent active Worker Assignments. Once accepted/cancelled and unlinked, the Session is retired from execution use.
- **Reviewer:** a fresh Session is used for exactly one review decision tuple: `(WorkItem, Worker Assignment ID/generation, Spec version, head SHA)`. It is never reused for another WorkItem, head, or later review. Its only allowed pre-review transcript is the verified bootstrap exchange from its own provisioning Attempt; unrelated history disqualifies it.

No Session may hold Owner/Coordinator and Worker/Reviewer roles for the same WorkItem. Worker cannot review its own Assignment; Reviewer cannot accept; Owner cannot be the Worker or Reviewer for the same slice. Different ChatGPT accounts are not required; Session and prompt/context separation is required. Session separation establishes agent-context independence, not independent human assurance.

A WorkItem has at most one current Worker Assignment generation and at most one active Reviewer Assignment for a given Worker Assignment/head. Worker base SHA and Reviewer target head SHA are immutable. A changed head or Spec creates a new Worker revision Assignment when implementation continues and always requires a fresh Reviewer Session/Assignment for the new exact head. New commits are appended as Evidence; they do not mutate an Assignment's head. A changed head or Spec requires a fresh Reviewer Session and decision.

### Authorization is separate from role

The Orchestrator/Owner is the only v1 actor with write-capable SO control-plane access and privileged GitHub mutation capabilities. Worker and Reviewer Sessions receive task/context packets and return implementation/review evidence; they do not receive the Owner SO credential or unrestricted SO write access. If the host cannot enforce per-Session connector ACLs, privileged SO/GitHub operations are mediated by the Orchestrator using restricted credentials. Prompts and role labels are not security boundaries.

An OwnerSessionRef is principal-bound, revocable, and generation-fenced. Revoking its grant/reference prevents future actions; historical Owner decisions remain immutable. The current SO MCP does not expose a verified caller ChatGPT conversation ID, so a caller-supplied SessionRef cannot itself authenticate the current conversation.

## 4. Attempt, dispatch outbox, and Sender Job

An Attempt is immutable SO intent for one deliberate remote side-effecting operation. It is not a second mutable execution state machine.

- SO creates the Attempt and dispatch-outbox intent in one SO DB transaction before enqueue.
- Attempt ID uses the trusted `att_` UUIDv4 form. It records WorkItem, Spec version, principal/tenant, operation, target role/Assignment ID and generation (preallocated for provisioning), exact prompt/request/context digests, encrypted payload reference, creation time, and one stable UUIDv4 Sender idempotency key.
- Sender Job is the authoritative execution lifecycle/transport receipt for that Attempt. There is exactly one Sender Job per Attempt. Sender Job carries the same immutable Attempt/WorkItem/Assignment/Spec correlation and exposes stage, side-effect certainty, failure stage, actual account, remote IDs, and output/evidence digests.
- Enforce unique `(tenant_id, principal_id, attempt_id)` in addition to Sender's idempotency key. Same Attempt with a different key is rejected. Same key + same payload returns the same Job; changed payload conflicts.
- Sender's idempotency tombstone and minimal receipt outlive Job-result retention until SO acknowledges the exact result/evidence digest and dependent event authorization is terminal. A 14-day Job deletion must not erase the only uniqueness record.
- The SO outbox is durable until Sender Job ID and receipt are reconciled. A lost enqueue response is recovered by replaying the same key and exact payload, never by minting a new key.

Attempt certainty is independent of Job status:

- `NOT_SUBMITTED`: evidence proves the consequential remote POST did not occur.
- `SUBMITTED`: the remote side effect was accepted/observed, even if later parsing or harvesting failed.
- `UNKNOWN`: the system cannot prove either outcome.

A proven `NOT_SUBMITTED` Attempt is terminal once its Sender Job has been created/executed. After the cause is corrected and the Assignment/account reservation is revalidated, a deliberate new execution uses a new Attempt ID, Sender Job, and idempotency key. The only same-Attempt/same-key replay is enqueue recovery when SO may have lost the enqueue response before recording the Job ID; Sender must return the original Job. V1 does not requeue terminal Sender Jobs in place. `SUBMITTED` and `UNKNOWN` are never resent or automatically retried. A known submission with incomplete reply is harvest-pending and may only be read/harvested. UNKNOWN holds its Assignment reservation and blocks new Attempts/reassignment until reconciled.

### Current remote operation shapes

- `session.create` is one consequential remote POST to `/backend-api/conversation` containing both conversation creation and the fixed bootstrap prompt (`ping`). SSE parsing, optional GET verification, harvest/archive, and SO landing follow that POST. One provisioning Attempt may cause at most one remote POST for its entire lifetime.
- HTTP 200 without one unique verified conversation ID is `remote_accepted_id_unresolved`: Attempt side-effect certainty is `SUBMITTED` (the remote POST was accepted), while Session identity resolution remains `UNKNOWN`; it is never a safe retry or account-rotation signal. If account echo is missing/mismatched but a candidate ID exists, preserve the ID and quarantine the orphan.
- `session.send` is one prompt submission against an existing confirmed Assignment Session. WorkItem v2 rejects a missing conversation ID. The legacy v0.1 compound create+bootstrap+task-send path is not a WorkItem Attempt.
- `session.read` is read-only and does not create an Attempt. Its result is reconciliation evidence only when tied to a verifiable Sender receipt or authoritative upstream snapshot/high-water/digest; one page or absence from one page does not prove no side effect.

Sender's AccountRouter reservation is authoritative. CLI must use strict account-only create and must not rotate inside an already reserved Job. If an account is unavailable before POST, return proven `NOT_SUBMITTED`; any deliberate new reservation is a separately authorized Attempt. Preserve HTTP status, submission stage, candidate IDs, account echo, response digest, and archive completeness independently of process exit code.

## 5. Assignment reservation and crash recovery

Before a remote provisioning side effect, SO preallocates immutable Assignment ID and monotonic generation and reserves the role slot using CAS in the same transaction as the Attempt/outbox. One canonical assignment-head/reservation projection per `(WorkItem, role)` tracks generation, active Assignment, provisioning Attempt, active Attempt, and revision. The projection is rebuildable from immutable events.

Enforce at most one active/provisioning Attempt per Assignment. UNKNOWN holds the reservation. Only positive NOT_SUBMITTED evidence releases it.

On SO restart, reconcile the reservation projection against immutable Assignment/Attempt events and Sender idempotency/receipt records. Explicitly distinguish: reservation with no Job; Job committed but enqueue acknowledgement lost; queued/running; terminal receipt pending; NOT_SUBMITTED; SUBMITTED; UNKNOWN; landing committed. Same-key replay recovers the existing Job. Missing/expired evidence never implies NOT_SUBMITTED. Startup must not create a new Attempt/Job merely because a projection is stale.

Every receipt, callback, reconciliation, and landing event carries tenant/principal, WorkItem revision, Spec version, Assignment ID/generation, Attempt ID, reservation ID, Sender Job ID, and payload/evidence digest. A delayed result from a superseded generation may be retained as historical evidence but cannot mutate current state, release a newer reservation, or create a current SessionLink.

## 6. Trusted landing and SessionLink

SessionLink is identity/lifecycle only; it is not Assignment, permission, or review evidence. Execution Sessions have at most one active WorkItem link. Owner/Coordinator assignments do not consume SessionLink cardinality.

Only a `session.create` provisioning Attempt may create a SessionLink. `session.send` references the existing Assignment/SessionLink and never creates or replaces it.

After SO verifies the Sender receipt, it atomically consumes the exact principal/tenant/WorkItem/Spec/Assignment-generation/Attempt/Job/reservation/Session/account/evidence-bound landing receipt, creates the Assignment, and creates one SessionLink. The trusted landing receipt is not a bearer grant. One immutable Attempt may create at most one SessionLink identity for its entire lifetime, even after unlink. Identical replay returns the original receipt/link; changed payload conflicts. A deliberate relink requires a new Attempt and Assignment generation or an explicit audited Owner recovery generation.

V0.1 link/unlink/context handlers cannot consume or expose v2 Attempt/Assignment records. Legacy/manual links are `unassigned_legacy`: they cannot satisfy Worker/Reviewer execution, review, or Acceptance. Generic WorkItem read never grants access to all linked Session contexts or IDs.

## 7. Evidence and Acceptance

Evidence is append-only and idempotent. Each Evidence record binds tenant/principal, source Attempt/Assignment/Spec, evidence kind, canonical content digest, and exact source references. Identical replay returns the same Evidence ID; conflicting payload or cross-WorkItem/Assignment substitution fails.

Required evidence includes:
- remote send/harvest receipt, remote message IDs, archive and reply digests;
- commit repository + full SHA and PR exact head SHA;
- test command/result and CI run ID/URL bound to the tested SHA;
- Reviewer Assignment, Worker Assignment, Spec version, reviewed head SHA, prompt digest, findings, and decision;
- authenticated Owner decision and current authorization reference.

Acceptance is a separate Owner decision, not a status update. One SO transaction CAS-checks WorkItem revision, Spec version, active Worker/Reviewer generations, exact accepted head SHA, required Evidence, hard dependency Acceptances, Owner authority, and absence of unresolved Attempts; it appends Acceptance, projects `completed`, and releases the exact SessionLink atomically. CI green, Worker self-report, memory retrieval, callback receipt, or Issue closure alone is never Acceptance.

GitHub Issue closure is a separate idempotent external action. Persist a durable closure intent with canonical Issue reference, expected Issue snapshot digest, Acceptance/WorkItem IDs, idempotency key, and any explicit Owner override/reason. If GitHub closes but SO loses the response, re-query and reconcile actual GitHub state; do not create another Acceptance. If the source Issue materially changed, revalidate and require a fresh Owner decision.

### Transaction boundaries and crash recovery (T1–T9)

All SO-owned WorkItem/Spec/Assignment/Attempt/Evidence/Acceptance rows share the SO database. Sender Job/owner rows remain in the Sender database. No transaction spans both databases or a remote HTTP request.

- **T1 — WorkItem creation:** one SO transaction authenticates the Owner principal, validates kind and canonical primary-Issue binding, creates WorkItem + immutable Spec v1 + owner/audit record, validates the dependency DAG, then commits. No remote Session is created.
- **T2 — Assignment reservation + Attempt/outbox:** one SO transaction validates current Spec/eligibility, preallocates Assignment ID and monotonic generation, reserves the role slot with CAS, stores Attempt ID + stable Sender UUIDv4 idempotency key + encrypted exact payload reference/digest/context manifest, appends the prepared transition and dispatch-outbox row, then commits. No remote side effect may precede this commit.
- **T3 — Sender enqueue:** outside the SO transaction, the dispatcher submits the exact payload with the stable key. Sender atomically stores Job + canonical payload digest/idempotency record. SO then records Job ID and acknowledges its outbox in a new transaction. If the enqueue response is lost, replay the exact same Attempt/key/payload and recover the same Job; never mint a new key for that transport-level recovery.
- **T4 — remote Session landing:** after session.create, SO independently verifies the exact conversation ID, trusted owner account/principal and complete bootstrap exchange. One SO transaction checks that Attempt/generation is current and unconsumed, WorkItem/Spec remains eligible, role cardinality holds, and the Session has no other active link; it consumes the exact one-time landing receipt and creates the Assignment + SessionLink + audit/transition records atomically. If any check fails, none of those rows commit; the remote Session is quarantined/unassigned for reconciliation.
- **T5 — Worker/Reviewer submit and harvest:** one SO transaction verifies Assignment/Spec/SessionLink and absence of another active Attempt for that Assignment, then stores the new Attempt + encrypted exact payload + outbox. Sender submit and reply harvest happen outside SO transactions and are separate evidence stages. HTTP 200 alone is remote acceptance, not verified reply evidence. Timeout after possible submission becomes UNKNOWN and cannot trigger another send.
- **T6 — Evidence and review:** append verified Worker output/commit/test/CI evidence against exact Assignment, Spec and head SHA. Only then provision a fresh Reviewer Assignment/Session for that exact review tuple. Reviewer output is append-only Evidence; the Orchestrator verifies read-only scope and exact head binding.
- **T7 — Acceptance:** one SO transaction rechecks current Spec, Worker/Reviewer generations, exact accepted/reviewed head SHA, required test/CI and Reviewer Evidence, hard dependencies, Owner capability and absence of unresolved Attempts; inserts immutable Acceptance, projects WorkItem completed, terminalizes the exact Assignment lifecycle and releases its SessionLink atomically. Direct work_item.update(status=completed) is forbidden.
- **T8 — GitHub Issue closure:** a separate idempotent GitHub operation after Acceptance. Re-read the Issue and all bound WorkItems; close only when every non-cancelled slice is accepted or an explicit Owner override exists. Persist closure intent and reconcile actual GitHub state after lost responses; never roll back Acceptance.
- **T9 — Session retirement/unlink:** for cancellation, supersession or other retirement not already performed by T7, one SO transaction rejects unlink while an active Assignment or unresolved Attempt depends on the Session; otherwise it terminalizes/supersedes the Assignment, appends unlink/audit events and closes the SessionLink atomically. No partial state.

Recovery invariants: crash before T2 commit leaves no Attempt/Job/remote side effect; crash after T2 resumes from outbox; crash after Sender enqueue but before SO acknowledgement recovers the same Job by the same key; crash after remote submit never resends UNKNOWN; crash after reply verification but before T4 landing repeats the idempotent one-time landing transaction; T7 commits Acceptance and completed projection together or neither; crash after T8 re-reads GitHub before retry. Missing or expired evidence never implies NOT_SUBMITTED.

All mutating SO operations require durable receipts keyed by tenant, authenticated principal, tool, stable request ID, and canonical argument digest. A caller timeout after execution begins is indeterminate, not proof of failure. Same key + same digest returns the original result; changed digest conflicts. Authorization, grant, role, and Assignment generation are revalidated immediately before commit.

## 8. Compatibility and migration

- Existing WorkItems with no verified type/Issue become migration-only `workflow_version=0 / legacy_unclassified`; do not relabel them all as operational. Preserve status, owner, grants, timestamps, SessionLinks, and landing history. Do not synthesize Assignments, Attempts, Evidence, or Acceptance.
- New WorkItems must be development or operational. `legacy_unclassified` cannot be newly created.
- Historical v0 rows already marked completed remain historical projections; migration does not fabricate Acceptance and those rows cannot authorize GitHub closure.
- V0.1 tools may retain non-terminal operational behavior only for workflow-version 0 rows. They reject v2 WorkItems/Attempts/Assignments. V0.1 link/unlink/context cannot mutate or access v2 lifecycle.
- `work_item.update(status=completed)` is forbidden for every version. Only the Acceptance transaction creates a new completed transition. Cancellation is a separate Owner-authorized durable record; generic write cannot cancel or auto-unlink all sessions.
- Public MCP schemas are versioned; do not silently mutate frozen v0.1 contracts.

## 9. Adapter and Events

Adapter Retrieve is optional context assistance, filtered to the exact WorkItem and requested Session/Assignment generation. A mismatched entry fails closed and is never exposed. Adapter cannot create/assign WorkItems, create Attempts, establish SessionLinks, grant authority, approve Evidence, or close Issues. Adapter MCP is out of scope.

Events are optional wake-up/notification only. They are never workflow state, dispatch authority, or Acceptance evidence. The Orchestrator may use durable polling and reconciliation; Events are not a prerequisite for core correctness.

## 10. Required negative, concurrency, and crash tests

1. Lost-response WorkItem create replay returns one WorkItem; changed payload conflicts.
2. Concurrent role provisioning reserves one Assignment generation and one active Attempt.
3. Same Attempt with a different Sender key is rejected; same key/payload returns one Job.
4. Crash after Sender Job commit but before SO acknowledgement recovers the same Job and receipt.
5. HTTP 200 without unique ConvRef, timeout after POST, and lost response produce SUBMITTED/UNKNOWN and zero additional POSTs.
6. A retryable timeout with matching remote prompt/reply is never resent.
7. T4 duplicate/stale/cross-principal/cross-Assignment landing and delayed old-generation callbacks are rejected.
8. Legacy link/context/unlink/list and generic read/write grants cannot cross principal, enumerate another Assignment's Session IDs, or mutate v2 lifecycle.
9. OwnerSessionRef revocation invalidates later calls; fabricated session/role/principal is denied.
10. Worker cannot merge/close; Reviewer cannot push/merge/close; Owner actions require current grants; Coordinator has no implicit Owner privilege.
11. Evidence and Acceptance identical replay return existing IDs; conflicting replay fails.
12. GitHub close succeeds but SO crashes: reconciliation records actual closure without duplicate Acceptance.
13. Inject crash after every point in the one-POST create → parse → GET/reconcile → harvest/archive → T4 landing flow; POST count per Attempt is at most one.
14. Startup reconstructs all reservation/Job/UNKNOWN/NOT_SUBMITTED/landing states without Session memory.
15. Run two complete WorkItem cycles, including Reviewer rejection, Worker repair under the same Worker Assignment, fresh Reviewer Session for the new head, Owner Acceptance, restart, and recovery.

## 11. Production gates and current disposition

Conceptual direction is accepted. The consolidated contract remains `ACCEPT_WITH_CHANGES`; production is BLOCKED and implementation is NOT AUTHORIZED until the P0 gates below are resolved and independently tested.

**P0:** #172 canonical SO/Sender source lineage and reproducible builds; #179 structured side-effect certainty and bounded reads; #183 Attempt-correlated Sender/landing; #208 one Attempt → at most one SessionLink; #196 Owner-only terminal/Acceptance/cancellation; #199/#201/#202 principal/Assignment-bound unlink/context/landing; #205 durable receipts for all SO mutations; #207 exact Adapter Session filter; #180/#198 revoked-account exclusion; #200 429 cooldown feedback; #188 filesystem isolation; #197 Job/idempotency/receipt/Event retention; #195 legacy Event backlog quarantine/reconciliation before any consumer activation (Events remain optional and disabled in core v1); server-enforced v2 and connector least privilege.

**P1:** #177/#204 scheduler yield, eligibility, fencing and bounded consumers; #185 authoritative read/reconciliation and coherent timeout budgets; #181 prompt/archive cleanup that preserves in-flight evidence; #164/#165/#36 Adapter contract pin and hard I/O limits; full negative/concurrency/crash matrix; actual connector ACL tests; two complete cycles and restart reconstruction.

No implementation, schema migration, tool-surface change, service restart, or production mutation is authorized by this ADR.
