# Security Acceptance Matrix — SO MCP surface (v1)

**Scope:** the Session Ops MCP surface on `main`. This paragraph says what the
document covers, not a revision it is frozen at: rows are added as behaviour
lands, so a commit pinned here describes the table on the day it was written and
nothing after. The line used to pin
`3839aeadf56cab4de3e477d28b477d1ae61f7cbc`, and that stopped being true — row 22
did not exist at that revision, and row 20 has since been extended to cover an
address-pinning fix that landed later. Where a claim depends on a revision, the
row names that revision.
Every row names the test that proves it. A row without a test name is not
evidence, it is an intention; any such row is marked `human:` and says why.

**What this matrix is for.** B1–B5 were five specific defects. Fixing them does
not by itself establish that the surface is safe; it only removes five known
holes. This matrix states, scenario by scenario, what an attacker or a mistake
is expected to achieve and what actually happens. It is a negative-test ledger,
not a feature list.

**Evidence types**

| Type | Meaning |
|---|---|
| `test:` | an offline case in the named file; the name is the assertion |
| `code:` | a symbol that implements the behaviour |
| `live:` | observed against a running deployment, with a date |
| `human:` | cannot be decided by an offline case; the reason is given |

**Reproduce**

```
python -m unittest tests.test_work_item_authority        # 50
python -m unittest tests.test_mcp_contract               # 28
python -m unittest tests.test_send_prompt_for_work_item  # 7
python -m unittest tests.test_mcp_transport              # 18
python -m unittest tests.test_mcp_alg_confusion          # 7
python -m unittest tests.test_mcp_rs256                  # 8
python -m unittest tests.test_mcp_sdk_integration        # 4 (skips without mcp)
python -m unittest tests.test_cdp_import_order           # 13
python -m unittest tests.test_security_matrix            # 7 (this file)
python -m unittest tests.test_landing_identity_contract  # 6
python -m pytest -q                                      # must be green; total not restated
```

The module counts above are the ones a reader can check in a single command: the
eight that back this table's rows, `test_security_matrix`, which validates the
table itself, and `test_cdp_import_order`, which backs no row here and is listed
only because it is the module whose arrival moved the number this block used to
carry. The whole-suite total is deliberately not restated. It moves whenever any
test anywhere is added, and the previous version of this block shows what that
costs — it was measured at `ac2b50c`, correct then, and wrong three hours later
when #50 added those same CDP import-order cases. The last recorded full-suite
snapshot is in `references/baselines.md` §G, with its own as-of date and commit;
it describes that commit and nothing after it.

These counts are checked by running the commands, not by reading the files:
`test_work_item_authority` builds its cases in loops, so counting `def test_`
in the source returns 8 for a module that reports 50, and a guard built on that
count would have been born red.

**What PROVEN means here, and what it does not.** PROVEN means the named case
asserts the behaviour *and* the case goes red when the behaviour is deleted.
That second half is not a formality: row 12 was PROVEN for three commits on a
test that stayed green after its guard was removed, and row 4 was CODE_ONLY
while the guard underneath it had never been executed by any test at all.

Rows marked `mutation-checked` have had that deletion actually performed. A row
without the mark would name and assert the behaviour while nobody had deleted the
implementation to watch it fail; it is weaker than a marked row and recorded as
such rather than averaged away. As of `a435f09` (#47) no row lacked the mark, so
the mark alone no longer separates a strong tier from a weak one — the qualifier
does. "for the claiming layer" and "for the lock" mean the deletion was performed
and one layer went red, while the rest of the row is covered by the same case
without being proven necessary; the note under each says which half is which.

**Erratum — the #30 commit title overstates this.** Commit `cca106c` is titled
"matrix v3 — every PROVEN row now has a deletion behind it". That is a commit
message, it is not rewritten, and it should not be read as a description of the
current table. The claim was already false when it was made: at `f28561b`
(2026-11-24) only 6 of the 22 rows then in the table had an actual deletion
behind them. How many have one today is deliberately not restated here — read
the `mutation-checked` mark in the status column, which is the only thing that
means `mutation-checked`. Per-row status in the table below is authoritative,
and restating a derivable count in prose is what produced this erratum.

What is worth stating instead is that a row can pass its deletion for the
wrong reason, and two did. The pinned-connection tunnel case went green when its
guard was deleted, because the real network dial failed with the same
exception type the case expected; and seven of the nine conjuncts in the
landing-confirmation expression cannot fail at all, because earlier
early-returns already guarantee them. Neither is visible in a green run, and
neither would have been found by counting marks.

---

## A. Identity and authentication (1–4b)

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 1 | A caller passes a principal, tenant or scope **as a tool argument** | Rejected. Identity comes from the verified token only. | `test:test_identity_cannot_be_injected_through_arguments` (test_mcp_contract) | PROVEN `mutation-checked` |
| 2 | No `Authorization` header, or a header the middleware cannot read | `UNAUTHENTICATED`. No tool dispatch. | `test:test_missing_auth_context_is_unauthenticated` | PROVEN `mutation-checked` |
| 3 | A structurally valid JWT whose `aud` is another service | Rejected at claim validation, and again in `principal_from_claims`. | `test:test_claim_mapping_rejects_wrong_audience` | PROVEN `mutation-checked` for the claiming layer; see note |
| 4 | `alg: none`, `alg: HS256`, or a `crit` header | Rejected before key lookup. Algorithm confusion is not reachable. | `test:test_none_algorithm_with_forged_signature` `test:test_hs256_using_rsa_public_modulus_as_hmac_key` `test:test_lowercase_rs256_is_rejected` `test:test_missing_alg_is_rejected` `test:test_crit_header_is_rejected_before_key_lookup` `test:test_unexpected_typ_is_rejected_before_key_lookup` (test_mcp_alg_confusion) | PROVEN `mutation-checked` |
| 4b | The signature itself: forged, wrong key, wrong hash, undersized or malformed key material | Rejected at verification. A genuine RS256 token is accepted and maps to the right principal. | `test:test_valid_rs256_signature_returns_claim_mapped_auth_context` `test:test_one_byte_signature_tampering_is_rejected` `test:test_signature_from_another_rsa_key_is_rejected` `test:test_sha1_signature_is_not_accepted_as_rs256` `test:test_1024_bit_rsa_modulus_is_rejected` `test:test_even_rsa_public_exponent_is_rejected` `test:test_rsa_public_exponent_one_is_rejected` `test:test_missing_cryptography_is_classified_as_dependency_unavailable` (test_mcp_rs256) | PROVEN `mutation-checked` |

**Row 2 note — where "No tool dispatch" is actually asserted.** The named case
calls `dispatch("work_item.create", {}, None, ...)` and asserts the error code;
`assert_error` checks four fields of the envelope and never looks at whether the
tool ran. The clause is true — `assert_error` sits behind the auth check — but
for the service path it is read from the code. The middleware path is the one
that asserts it: row 21's cases compare `inner_calls` against `[]` after a 401,
so a request that reached the inner app would turn them red. Row 1, which
asserts `self.store.calls == []` directly, is the shape this row is missing.

**Row 3 note.** `aud` is checked twice: once in `validate_registered_claims`
and again in `principal_from_claims`. The cited test exercises
`principal_from_claims`. Deleting the `aud` check there fails it; deleting the
one in `validate_registered_claims` leaves the suite green, confirmed again in
this pass. The behaviour is covered once, which is enough for the behaviour, but
the row previously named the wrong layer as the proof. The mark therefore reads
"for the claiming layer": the layer that is actually observed.

The duplicate is not dead code and it is not a defect. It is the same shape as
row 16's `rowcount` guard — a second check that no test can distinguish, kept
because the cost of keeping it is one comparison and the cost of being wrong
about the first layer is an accepted token from another service.

**Row 4 note — stale, corrected.** An earlier revision of this document said
`alg` rejection was CODE_ONLY because no offline case fed a forged token
through `verify()`. That was true when written and stopped being true when
`test_mcp_alg_confusion` landed: seven cases now drive the header checks, and
deleting the `alg` guard fails four of them.

The stale sentence survived because the row's Status cell had already been
updated while its note had not. A note that contradicts its own row is worse
than no note: it is a false statement inside the document whose whole purpose
is to not make false statements. Left here, struck through in substance, so the
next reader knows the correction happened rather than assuming the note is
current.

**Row 4b note — the largest gap this pass found.** Row 4 covered the algorithm
guards, and every one of its cases rejects *before a key is fetched*. So the
one step that decides whether a token is genuine was never executed by any test
in the repository. Deleting the entire `public_key.verify(...)` call left the
suite at 711 green.

That is not a coverage percentage, it is a category error: signature
verification was code, not behaviour. Row 4 and row 4b are separate rows because
they answer separate questions — "is this the right algorithm" and "did the
holder of the private key actually sign this".

Mutation-checked four ways: deleting the verify call fails the three forgery
cases; swapping its two arguments fails the positive control; replacing
PKCS1v15 with PSS fails the positive control. The positive control matters as
much as the rejections: without it, a rejection could come from anywhere.

---

## B. Authorization (5–8)

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 5 | Authenticated principal lacks a required scope for the tool | `PERMISSION_DENIED`, distinct from `UNAUTHENTICATED`. | `test:test_insufficient_scope_is_permission_denied` | PROVEN `mutation-checked` |
| 6 | `work_item.grant` / `work_item.revoke` called with only `work_item.write` | Denied. Delegation administration needs `session_ops.access.manage`. | `test:test_work_item_grant_requires_access_manage` `test:test_work_item_revoke_requires_access_manage` | PROVEN `mutation-checked` |
| 7 | A `write` grantee tries to grant to a third party, or revoke another principal's grant | Denied. `write` never implies grant administration. | `test:test_write_grantee_cannot_grant_or_revoke` | PROVEN `mutation-checked` |
| 8 | Owner grants to a target that is unknown, disabled, or in another tenant | Refused **before** the grant row is written; cross-tenant attempts leave no row. | `test:test_unknown_target_is_rejected_by_default_directory` `test:test_same_tenant_unregistered_target_is_rejected` `test:test_target_must_exist_be_enabled_and_be_same_tenant` `test:test_cross_tenant_target_is_rejected_before_grant_is_written` | PROVEN `mutation-checked` |

**Row 6 note.** The grant half was already held. The revoke half was not: the
cited case only ever called `work_item.grant`, so deleting
`session_ops.access.manage` from `TOOL_REQUIRED_SCOPES["work_item.revoke"]`
left all cases green. A symmetric revoke case now exists and goes red on that
deletion.

**Row 8 note — a fourth refusal that no case can reach.** `_resolve_grant_target`
also refuses when `record.principal_id != target`. Both directories bundled with
this tree key their lookup by principal id — `StaticPrincipalDirectory.resolve`
is `self._records.get(principal_id)`, and `DenyAllPrincipalDirectory` returns
nothing — so a record answering for a different id cannot occur with either. The
clause defends against a `PrincipalDirectory` implementation that replies with the
wrong principal, which the Protocol permits and neither bundled implementation
does. Mutation check: deleting the clause leaves `tests/test_work_item_authority`
green. So this row's "unknown, disabled, or another tenant" names three of the
four checks the function performs; the fourth is defence in depth, the same shape
as row 3's duplicate `aud`.

---

## C. Isolation (9–11)

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 9 | Same-tenant principal with no grant reads another's WorkItem | Denied. | `test:test_same_tenant_stranger_cannot_read` | PROVEN `mutation-checked` |
| 10 | Another tenant reads, and separately: the WorkItem does not exist | **Indistinguishable.** A caller must not be able to probe for existence. | `test:test_other_tenant_cannot_read` `test:test_missing_cross_tenant_and_no_grant_are_identical` `test:test_cross_tenant_and_missing_resource_are_indistinguishable` `code:NotAuthorized()` (empty message) | PROVEN `mutation-checked` |
| 11 | A `read` grant tries to update status | Denied; `read` does not imply `write`. | `test:test_read_grant_allows_read_but_not_write` `test:test_write_grant_allows_status_update` | PROVEN `mutation-checked` |

**Row 10 note — one branch with nothing behind it.** `_require_owner` also raises
when the `work_item_owners` lookup returns no row. That is reachable: an owner row
can be absent, which is what `OrphanReport` exists to describe. But no offline
case builds one, so replacing the clause with `False` leaves the suite green.
This is **not** the unreachable code that row 16 records — the `rowcount` guard
there cannot be executed at all, while this branch can and simply never is. It is
listed as an uncovered branch rather than counted as evidence.

---

## D. Session linking and F-13 (12–15)

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 12 | Caller constructs its own landing evidence and passes it to `link_session` | Rejected outright **even when a valid unclaimed landing event exists**. `LandingEvidence` no longer exists in the source; any non-`None` `evidence` raises. | `test:test_caller_supplied_evidence_is_rejected_even_with_valid_landing` `test:test_caller_supplied_evidence_is_rejected` `test:test_caller_supplied_evidence_is_rejected_by_authority` `test:test_plain_ids_convref_and_booleans_are_not_evidence_inputs` `code:link_session` (`if evidence is not None: raise`) | PROVEN `mutation-checked` |
| 13 | No server-recorded landing event exists for the conversation, or the event that exists is bound to a different `attempt_id` or a different WorkItem | `QuarantineNotCleared`. The event stays unconsumed and unrejected: it belongs to the attempt that produced it. | `test:test_link_requires_server_recorded_landing` `test:test_unconfirmed_f13_evidence_is_rejected` `test:test_t2_landing_for_attempt_one_cannot_be_consumed_by_attempt_two` `test:test_t1_landing_for_work_item_a_cannot_be_consumed_by_work_item_b` | PROVEN `mutation-checked` |
| 14 | A confirmed event is consumed and then replayed; or a second landing arrives for a session that already holds an active link | The replay fails. Consumption and link creation happen in one transaction. The second landing is rejected terminally — `rejected_at` and `rejection_reason = 'session_already_linked'` are written in a separate transaction after the link transaction rolls back, and a failure to write them propagates instead of marking the event rejected. | `test:test_confirmed_complete_landing_can_be_consumed_once` `test:test_event_replay_cannot_create_a_second_link` `code:link_session` (`consumed_at IS NULL` → `UPDATE consumed_at` → `INSERT session_links`) `test:test_t3_duplicate_active_session_link_rejects_second_landing_terminally` `test:test_t6_failure_while_recording_rejection_propagates_and_does_not_mark` | PROVEN `mutation-checked` |
| 15 | Landing is `mismatch`, `ambiguous`, or evidence incomplete; or the send returned 409 | No event recorded, no link. Fail closed. | `test:test_mismatch_ambiguous_and_incomplete_landing_fail_closed` `test:test_mismatch_409_does_not_record_or_link` `test:test_ambiguous_multiple_ids_do_not_record_or_link` `test:test_incomplete_reply_does_not_record_or_link` `test:test_missing_archive_file_does_not_record_a_landing_event` | PROVEN `mutation-checked` |

**Row 12 note.** The original evidence for this row was
`test_caller_supplied_evidence_is_rejected`, which seeds no landing event — so
the *neighbouring* check (`landing is None`) raised first, and deleting the
`evidence is not None` guard left the test green. It still cannot see its own
subject: `f9d88c5` made `attempt_id` the fourth positional parameter, and that
case passes a dict where the id belongs, so it now stops at the attempt-id shape
check before reaching either guard. The row is PROVEN on
`test_caller_supplied_evidence_is_rejected_even_with_valid_landing`, which seeds
a landing, passes a real attempt id, and asserts the refusal plus
`consumed_at is None`, `rejected_at is None` and `link_count == 0`.

The replacement seeds a confirmed, complete, unconsumed event first, asserts the
refusal, asserts the event is still unconsumed and no `session_links` row was
written, and then links the same session without evidence to prove the seeded
event was usable. Mutation check: with the guard deleted, the old case stays
green and only the new one fails.

**Row 13 note — a clause that no case watched.** The Expected cell used to add
"Nothing is written." Both named cases assert the refusal and stop there:
`test_link_requires_server_recorded_landing` raises and never looks at
`session_links`, and `test_unconfirmed_f13_evidence_is_rejected` asserts the
error string `QUARANTINED_SESSION`. Delete the landing requirement and both go
red, so the refusal is held. Reverse the order inside `link_session` — insert the
link, then raise — and both stay green while a quarantined session is linked.
The property is true of the code: the guard returns before the first `INSERT`.
It was never true of a test, so it is not in the table any more. Row 12's landing
case does assert `link_count == 0`, for its own path; nothing does so for this
one, and that asymmetry is now visible in how the two rows read.

`f9d88c5` then bound the event to a `(work_item_id, attempt_id)` pair, and the
binding is enforced **three times**: filtered in the landing `SELECT`, filtered
again in the consuming `UPDATE`, and the `consumed != 1` check turns a lost race
into `QuarantineNotCleared`. Removing any one of the three leaves the whole
suite green; removing the filter from all three turns `t2` red, twice in a row,
and the WorkItem half behaves the same way on `t1`. That is redundancy, not
coverage: an edit that deletes one filter and keeps two is caught by nothing.

The attempt id is also checked by shape before any query — `att_` plus a
canonical lowercase UUIDv4, in `link_session` and again in
`_record_trusted_landing`. Deleting that check leaves the suite green *except*
for `test_caller_supplied_evidence_is_rejected`, which passes a dict where the
attempt id belongs: the old blind case from row 12 now holds a new guard by
accident. Repairing that test to pass a real attempt id would silently unhold
the shape check, which is why the coupling is written down here rather than
discovered by whoever does the repair.

**Row 14 note — the rejection is a second transaction, and the cases know it.**
`reject_after_rollback` opens its own connection, takes `BEGIN IMMEDIATE`,
updates the exact pending row and commits; a failure to persist is re-raised
rather than swallowed, because the caller must not report REJECTED unless that
transaction committed. Replacing both call sites with a no-op turns `t3` and
`t6` red, twice each. Deleting the `rejected_at IS NULL` predicates everywhere —
ten sites — also turns `t3` red, and that is what makes "terminal" mean
terminal: a rejected event is never reconsidered.

Both rows were re-measured on `f9d88c5` with a harness that collects `SUBFAILED`
as well as `FAILED`. The earlier version read only `FAILED`, which reports a
`subTest` failure as a pass; every value in these two notes was taken after that
fix, and one mutation that the broken harness called green
(`match = 'confirmed'` removed at six sites) is in fact red on this row's
`test_mismatch_ambiguous_and_incomplete_landing_fail_closed`.

**Row 15 note — nine conjuncts, two of them load-bearing.** The landing
decision in `scripts/chatgpt_send.py` is a conjunction of nine conditions.
Deleting them one at a time: seven leave the file green, and only
`reply_complete is True` and `os.path.isfile(path)` change the outcome.

The seven that cannot fail are not a testing gap — they are guaranteed by
earlier early-returns in the same function, so the condition can never be
reached with them false. `st == 200` because `st != 200` returned above;
`len(all_cids) == 1` because a longer list returned above; `not mismatch` for the
same reason; `first_cid == real_conv` because `real_conv` *is* `first_cid`;
`all_cids[0] == real_conv` because the list has one element by then; `bool(path)`
because `isfile` implies it; `(not conv_id or conv_id == real_conv)` because
`mismatch` is `bool(conv_id) and real_conv != conv_id` and that branch already
returned, so the disjunction is true before it is evaluated. They are defence in
depth, and deleting them is an equivalent mutant.

The one real gap this exposed: `os.path.isfile(path)` had **no** case behind it.
`FakeAPI.save_reply` always writes the file, so "a path was returned" and "a
file exists" were the same thing in every test. They are not the same thing in
production — `_atomic_write` can fail after the path is computed. A case now
returns a path that was never written and asserts no event is recorded and the
session cannot be linked.

| Deleted conjunct | Result |
|---|---|
| `reply_complete is True` | `test_incomplete_reply_does_not_record_or_link` red |
| `os.path.isfile(path)` | `test_missing_archive_file_does_not_record_a_landing_event` red |
| the other seven | green — equivalent mutants, each guaranteed by an earlier return |

---

## E. Atomicity and audit (16–17)

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 16 | A concurrent revoke lands between the authorization check and the mutation; or a relink moves the session between the unlink check and the delete | Both fail closed. Check and mutation share one `BEGIN IMMEDIATE` on one connection. | `test:test_begin_immediate_blocks_second_writer_before_it_reads` `test:test_stale_expected_link_id_does_not_unlink` `test:test_expected_updated_at_is_compare_and_swap` | PROVEN `mutation-checked` for the lock; see note |
| 17 | The audit write fails partway through a mutation | The business change rolls back too. No unaudited mutation survives. | `test:test_audit_failure_rolls_back_work_item_and_owner` `test:test_audit_failure_rolls_back_status_mutation` `test:test_all_successful_mutations_are_audited` `test:test_audit_rows_are_append_only` | PROVEN `mutation-checked` |

**Row 16 note — the lock is proven, the `rowcount` guard is not.** Two claims, two different strengths.

The lock is now proven. `test_begin_immediate_blocks_second_writer_before_it_reads`
holds a writer reservation on one connection and drives `link_session` on a
second; it asserts the second fails at acquisition *and* that the trace callback
recorded exactly one statement. The trace assertion is what makes it
mutation-proof: with deferred `BEGIN` the second connection would issue its
SELECTs first and only fail later, which a bare `assertRaises` would accept.
Deleting `BEGIN IMMEDIATE` turns it red with five extra statements recorded.

The `rowcount` guard is a different matter: `if changed != 1: raise` in
`unlink_session` is **unreachable**. `BEGIN IMMEDIATE` is acquired before the
SELECT, and that SELECT already filters `unlinked_at IS NULL`, so under SQLite's
single-writer rule no concurrent write can land between them and `rowcount` is
always 1. It is defence in depth, not evidence. No test can fail when it is
deleted because no execution can reach it. Recorded as dead code rather than
counted as a proof.

**Row 16, continued — what the cases prove, and what they do not.** The offline cases
prove the *precondition* mechanism (a stale `expected_link_id` or
`expected_updated_at` is refused) and read the code to confirm the transaction
shape. They do **not** interleave two real threads and observe the race. A true
concurrency test would need two connections and a deliberate pause; there is
none. What is proven is "the mutation is guarded and the guard is evaluated
inside the same transaction", not "the race was reproduced".

---

## F. Degradation (18)

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 18 | Memory adapter is unconfigured; or it returns an oversized response | `DEPENDENCY_UNAVAILABLE` and `RESOURCE_EXHAUSTED` respectively. A degraded dependency must not be reported as an empty success, and an oversized payload must not be silently truncated. | `test:test_context_without_adapter_is_dependency_unavailable` `test:test_oversized_session_context_becomes_resource_exhausted` `test:test_oversized_response_becomes_resource_exhausted` `test:test_session_context_with_injected_adapter` | PROVEN `mutation-checked` |

**Row 18 note.** This row used to bundle two independent oversize paths behind
one citation, and the citation was for the wrong one.
`test_oversized_response_becomes_resource_exhausted` drives `work_item.create`
through a fake store and exercises the envelope-size check at the end of
`dispatch`; it never touches the adapter.

The adapter path had two problems. Its `MAX_CONTEXT_CONTENT_CHARS` branch was
unreachable by any test, and when reached it raised `RuntimeError`, which mapped
to `INTERNAL` — while this row and the envelope path both promised
`RESOURCE_EXHAUSTED`. One condition, two codes, no uniform client handling.
Now a dedicated `_ResourceExhausted` maps to `RESOURCE_EXHAUSTED`, and
`test_oversized_session_context_becomes_resource_exhausted` drives
`session.context` itself. Short-circuiting the branch to `False` returns the
oversized payload to the client verbatim and turns that case red.

**Row 18, continued — the happy path proves nothing about the failure branch.** `test_session_context_with_injected_adapter` is the happy path
and does not prove the fail-closed branch. That branch is
`test_context_without_adapter_is_dependency_unavailable`: deleting the
`memory_adapter_command is None` guard turns its expected
`DEPENDENCY_UNAVAILABLE` into `INVALID_ARGUMENT` and it goes red. Checked.

---

## G. Transport and discovery (19–22)

These were absent from v1. An independent review of the deployed surface found
the three holes in rows 19–21; they are not new features, they are repairs to
code that was already running. Row 22 covers a different risk — an assumption
about the pinned SDK, not a hole an attacker walks through.

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 19 | An unauthenticated caller sends many random `kid` values to amplify JWKS fetches | Fresh-cache misses fail without fetching. Expired-cache refresh is single-flight and backs off after failure. The lock is never held across network I/O. | `test:test_fresh_cache_unknown_kid_never_downloads` `test:test_expired_cache_concurrent_refresh_is_single_flight` `test:test_failed_refresh_is_not_retried_inside_backoff_window` `test:test_refresh_can_retry_after_backoff_expires` (test_mcp_transport) | PROVEN `mutation-checked` |
| 20 | An attacker-influenced issuer points discovery at an internal address | Redirects refused. HTTPS destinations resolved once; any non-global answer rejects the whole URL. The connection then dials the answer that was validated, keeps the hostname for SNI and certificate verification, and does not consult a system proxy. | `test:test_non_override_discovery_rejects_different_jwks_origin` `test:test_default_fetcher_does_not_follow_redirects` `test:test_https_jwks_rejects_loopback_link_local_and_private_ips` `test:test_jwks_rejects_all_dns_answers_if_any_is_non_global` `test:test_resolve_validated_address_returns_the_pinned_answer` `test:test_resolve_validated_address_rejects_mixed_answers` `test:test_validate_public_https_url_still_returns_the_url` `test:test_pinned_connection_dials_the_ip_and_keeps_the_hostname_for_sni` `test:test_pinned_connection_refuses_a_proxy_tunnel` `test:test_default_fetcher_installs_no_proxy_handler` `test:test_default_fetcher_pins_the_validated_address` (test_mcp_transport) | PROVEN `mutation-checked` |
| 21 | A request targets the MCP endpoint's subpaths | Authenticated. Health and protected-resource metadata stay public. | `test:test_mcp_subpath_is_authenticated` `test:test_mcp_endpoint_itself_is_authenticated` `test:test_health_and_protected_resource_metadata_remain_public` (test_mcp_transport) | PROVEN `mutation-checked` |
| 22 | The pinned SDK's API drifts from what `build_mcp_server` assumes | Build fails at CI, not at deploy. `Server`, `on_list_tools`, `on_call_tool`, `streamable_http_app` keywords and `CallToolResult.structured_content` are all exercised against the real `mcp==2.3.0`. | `test:test_builds_real_starlette_app_at_verifier_audience_path` `test:test_real_sdk_registers_list_and_call_handlers` `test:test_real_tools_list_returns_the_frozen_tool_surface` `test:test_real_tools_call_rejects_missing_request_authentication` (test_mcp_sdk_integration) | PROVEN `mutation-checked` |

**Row 19 note — three guards, three deletions.** Fresh-cache miss
(`test_fresh_cache_unknown_kid_never_downloads`), single-flight
(`test_expired_cache_concurrent_refresh_is_single_flight`) and the backoff
window (`test_failed_refresh_is_not_retried_inside_backoff_window`) each go red
on their own deletion. The fourth claim — the lock is never held across network
I/O — is asserted by reading the code, not by a case, so it is not counted.

**Row 20 note — "partial" was two gaps, and only one was written down.** The
recorded gap was DNS rebinding: validation and connection were separate
resolutions, so a hostile resolver could answer public the first time and
private the second. Measuring it turned up a second, worse one. `build_opener`
installs `ProxyHandler` by default and `urllib` reads the system proxy
configuration; on Windows that lives in the registry, so not one environment
variable hints at it. With a proxy configured the process never resolves the
hostname — the proxy does — and the address check describes a host nothing ever
contacts. Measured here with every proxy env var unset: `getproxies()` returned
`http://127.0.0.1:7000`, validation resolved `issuer.example.test` once, and the
connection went to `('CONNECT', ('127.0.0.1', 7000))`. The SSRF check was inert
and the whole file was green. CI never sees that one; it sees only the
rebinding gap, which is why the gap table never had a second row.

Both are fixed in one place. `_resolve_validated_address` returns the address it
checked, `_PinnedHTTPSConnection` dials that address and passes the original
hostname to `wrap_socket` so SNI and certificate verification are unchanged,
the proxy handler is replaced with an empty map, and proxy tunnels are refused
outright. `_validate_public_https_url` still returns the URL because the
discovery-origin comparison calls `urlparse()` on its return value; changing
that contract to return an address would have crashed discovery.
`test_validate_public_https_url_still_returns_the_url` now holds that line.

Four mutations, four different named cases red, one failure each:

| Deleted | Case that went red |
|---|---|
| `urllib.request.ProxyHandler({})` from the opener | `test_default_fetcher_installs_no_proxy_handler` |
| `server_hostname=self.host`, replaced with the pinned IP | `test_pinned_connection_dials_the_ip_and_keeps_the_hostname_for_sni` |
| pinning, replaced by validate-then-let-the-client-resolve | `test_default_fetcher_pins_the_validated_address` |
| the tunnel refusal in `connect()` | `test_pinned_connection_refuses_a_proxy_tunnel` |

The last one is worth a sentence because its first draft was a false positive.
It asserted only `assertRaises(OSError)`. Delete the guard and the real dial to
`93.184.216.34:443` fails with `OSError` too: green, for the wrong reason, 21
seconds slow. It now asserts that nothing was dialled, which is the behaviour
the guard actually provides. A test that passes when the guard is gone is worse
than no test, because it occupies the slot where the real one belongs.

**Row 22 note — the assumption we shipped on.** Before this row existed, every
claim `build_mcp_server` makes about `mcp==2.3.0` was verified by hand once.
`test_mcp_transport` builds the app against hand-written `ModuleType` stubs, so
CI validated our *belief* about the SDK, not the SDK. Renaming a keyword in
`streamable_http_app` would have shipped green and failed at first deploy.

An earlier draft of this work recorded a wrong conclusion: the PyPI README shows
`from mcp.server import MCPServer`, which was read as "`Server` was removed".
Reading the wheel settles it — `mcp/server/__init__.py` still exports `Server`
from `.lowlevel`, and `MCPServer` is an added high-level wrapper, not a
replacement. **README is the marketing surface; `__init__.py` is the contract.**

One mutation was deliberately left green: renaming `structured_content` to
`structuredContent` changes nothing, because the SDK sets
`alias_generator=to_camel` with `populate_by_name=True`. That is an equivalent
mutant. Recorded so it is not mistaken for a weak case on a re-run.

---

## What this matrix does not cover

These are gaps, stated so nobody reads the rows above as completeness.

| Gap | Why it is not covered | Consequence |
|---|---|---|
| In-process code execution | Python privacy is not a boundary. `_record_trusted_landing` is a plain method; anything running in the SO process can call it and can also write the SQLite file directly. | B1 is closed against a **remote** caller only. Requires deployment isolation. |
| Landing events never expire | `authority_landing_events` has `created_at` but no expiry, and nothing sweeps pending rows. | A confirmed event that was never consumed can still be spent much later by the attempt that knows its `attempt_id`. That needs the id and `write` on the WorkItem, so it is a replay window rather than a bypass. Stated, not closed. |
| A mandatory corporate proxy | `ProxyHandler({})` is passed unconditionally, so a deployment that can only reach the identity provider through a proxy fails to fetch JWKS. | Fail closed, deliberately: the alternative is an address check describing a host the process never contacts. Stated here rather than solved. |
| The pinned address has no failover | Only the first validated answer is dialled. A host with several public addresses loses the rest if the first one is down. | Availability, not security. Retrying a second answer would reintroduce the resolution the pin exists to remove. |
| Mutation coverage is not uniform | Row 16 carries the mark for its lock only, and row 3 for the layer its case actually observes. Every other row carries it outright. | The two that do not are named individually with a reason. Treat an unmarked claim as "asserts the behaviour", not "proven necessary" — that is exactly how row 12 stayed PROVEN for three commits. |
| Seven landing conjuncts cannot fail | Row 15's landing expression has nine conjuncts; seven are already guaranteed by earlier early-returns, so deleting them changes nothing. | Equivalent mutants, listed one by one in the row 15 note rather than counted as coverage. The two load-bearing ones each have a deletion behind them. |
| Security tests can skip to green | `test_mcp_rs256` and `test_mcp_sdk_integration` both skip as units when their optional dependency is absent. The unit job installs `requirements-mcp.txt` and asserts the skip count is unchanged, printing every skip reason on failure; the `sdk` job asserts its four cases executed. | Before those guards, the signature path and the SDK compatibility surface could both be skipped and the log still read green. Any future optional dependency needs the same treatment. |
| `unlink_session` `rowcount` guard is unreachable | `BEGIN IMMEDIATE` precedes the SELECT and the SELECT filters `unlinked_at IS NULL`, so SQLite's single-writer rule makes `rowcount != 1` impossible. | Defence in depth, not evidence. It cannot be tested, and pretending otherwise would be worse than saying so. |
| `aud` is checked twice, once unheld | Deleting the `validate_registered_claims` check changes no test outcome. | Redundant layer. Harmless, but not a second proof. |
| Real thread-level races | No test interleaves two connections. | Row 16 proves the guard, not the race. |
| Multi-user isolation | D4 fixes the first deployment at one principal, one tenant. | Cross-principal/cross-tenant behaviour is tested at the unit level only; it is not claimed for a multi-user deployment. |

---

## Change control

A row may move from CODE_ONLY or human to PROVEN only by adding a named offline
case. Deleting a row requires the same review as deleting a test. New security
behaviour must arrive with its row, in the same change.

A row may be marked `mutation-checked` only after the guard has actually been
deleted and the row observed to go red. The deletion, the failing case names and
the restored state must be in the commit message or the PR body. Asserting that
a test "would" fail is not a mutation check.

A note goes **below** its section's table, never between two rows. A blank line
inside a table ends it: the rows after the note render as literal text while a
line-scanning parser still reads them as rows, so the data checks stay green on
a document that no longer displays. `test_every_row_stays_inside_a_real_table`
holds that line; it is a document check, not a security row, which is why it is
not cited from the table above.

---

## Mutation log — 2026-11-24

Every deletion below was performed on this tree, the suite was run, and the tree
was restored (`git diff --stat` clean afterwards). Where the suite stayed green,
the row was downgraded.

| Deleted / changed | Full suite | Consequence |
|---|---|---|
| `if evidence is not None: raise` in `link_session` | 1 failed — the new row 12 case only; the old case stayed green | Row 12 could not see its subject. Replaced. |
| `if header.get("alg") != "RS256"` | 4 failed — none-forged, HS256, lowercase, missing | Row 4 was CODE_ONLY. Now PROVEN. |
| `if "crit" in header:` | 1 failed — crit case | Row 4 crit half now covered. |
| `memory_adapter_command is None` guard | 1 failed — `DEPENDENCY_UNAVAILABLE` became `INVALID_ARGUMENT` | Row 18 unconfigured half is genuine. |
| `session_ops.access.manage` removed from `work_item.revoke` | **all 707 green** at the time; now 1 failed — the new revoke case | Row 6 revoke half was unheld. Closed. |
| `aud` check in `principal_from_claims` | 1 failed | Row 3 behaviour is covered here. |
| `aud` check in `validate_registered_claims` | **all 707 green** | Second `aud` layer unheld. Noted on row 3. |
| all 7 `BEGIN IMMEDIATE` → `BEGIN` | **all 707 green** at the time; now 1 failed — the two-connection case, with five extra statements traced | Row 16 lock claim was unheld. Closed. |
| `if changed != 1: raise` in `unlink_session` | **all 707 green** | Unreachable, not unheld. Dead code; cannot be tested. |
| `public_key.verify(...)` call deleted from `verify_rs256_signature` | **all 711 green** at the time; now 3 failed — tampered, wrong key, SHA-1 | Signature verification was executed by nothing. Row 4b added. |
| `verify(signature, signing_input, ...)` arguments swapped | 1 failed — the positive control | Row 4b. |
| `PKCS1v15()` replaced with `PSS(...)` | 1 failed — the positive control | Row 4b. |
| adapter oversize condition short-circuited to `False` | 1 failed — the `session.context` oversize case; the payload returned verbatim | Row 18 adapter half was unheld. Closed. |
| `streamable_http_path=endpoint_path` → `"/wrong"` | 1 failed — the audience-path case | Row 22 route claim is held. |
| `is_error=not envelope["ok"]` → `is_error=False` | 1 failed — the unauthenticated `tools/call` case | Row 22 envelope claim is held. |
| `structured_content=envelope` → `structuredResult=envelope` | 1 failed — pydantic rejects the unknown field | Row 22 field-name claim is held. |
| `structured_content=envelope` → `structured_content=None` | 1 failed — the client asserts the field is a dict | Spelling is not the claim; the envelope must arrive. |
| `for name in TOOL_SCHEMAS` → `list(TOOL_SCHEMAS)[:-1]` | 1 failed — the tool-surface case | Row 22 surface claim is held. |
| `structured_content` → `structuredContent` | **all 4 green** | Equivalent mutant: SDK sets `alias_generator=to_camel` with `populate_by_name=True`. Both spellings are valid. Not a gap. |

The two lines that still read "all green" are the residue, and they are not the
same kind of thing. The `aud` one is a redundant second layer with the behaviour
covered elsewhere. The `rowcount` one is unreachable code: no test can fail when
it is deleted, so listing it as evidence would be a lie in the other direction.
