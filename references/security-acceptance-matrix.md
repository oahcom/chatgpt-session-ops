# Security Acceptance Matrix — SO MCP surface (v1)

**Scope:** the Session Ops MCP surface as it exists at `2c67d63` on `main`.
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
python -m unittest tests.test_send_prompt_for_work_item  # 6
python -m unittest tests.test_mcp_transport              # 11
python -m unittest tests.test_mcp_alg_confusion          # 7
python -m unittest tests.test_mcp_rs256                  # 8
python -m pytest -q                                      # 719 passed, 6 skipped
```

**What PROVEN means here, and what it does not.** PROVEN means the named case
asserts the behaviour *and* the case goes red when the behaviour is deleted.
That second half is not a formality: row 12 was PROVEN for three commits on a
test that stayed green after its guard was removed, and row 4 was CODE_ONLY
while the guard underneath it had never been executed by any test at all.

Rows marked `mutation-checked` have had that deletion actually performed. Rows
without the mark have a case that names and asserts the behaviour, but nobody
has yet deleted the implementation to watch it fail. They are weaker than
`mutation-checked` even though both currently read PROVEN, and the difference
is recorded per row rather than averaged away.

---

## A. Identity and authentication (1–4)

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 1 | A caller passes a principal, tenant or scope **as a tool argument** | Rejected. Identity comes from the verified token only. | `test:test_identity_cannot_be_injected_through_arguments` (test_mcp_contract) | PROVEN |
| 2 | No `Authorization` header, or a header the middleware cannot read | `UNAUTHENTICATED`. No tool dispatch. | `test:test_missing_auth_context_is_unauthenticated` | PROVEN |
| 3 | A structurally valid JWT whose `aud` is another service | Rejected at claim validation, and again in `principal_from_claims`. | `test:test_claim_mapping_rejects_wrong_audience` | PROVEN, second layer unproven |
| 4 | `alg: none`, `alg: HS256`, or a `crit` header | Rejected before key lookup. Algorithm confusion is not reachable. | `test:test_none_algorithm_with_forged_signature` `test:test_hs256_using_rsa_public_modulus_as_hmac_key` `test:test_lowercase_rs256_is_rejected` `test:test_missing_alg_is_rejected` `test:test_crit_header_is_rejected_before_key_lookup` `test:test_unexpected_typ_is_rejected_before_key_lookup` (test_mcp_alg_confusion) | PROVEN `mutation-checked` |
| 4b | The signature itself: forged, wrong key, wrong hash, undersized or malformed key material | Rejected at verification. A genuine RS256 token is accepted and maps to the right principal. | `test:test_valid_rs256_signature_returns_claim_mapped_auth_context` `test:test_one_byte_signature_tampering_is_rejected` `test:test_signature_from_another_rsa_key_is_rejected` `test:test_sha1_signature_is_not_accepted_as_rs256` `test:test_1024_bit_rsa_modulus_is_rejected` `test:test_even_rsa_public_exponent_is_rejected` `test:test_rsa_public_exponent_one_is_rejected` `test:test_missing_cryptography_is_classified_as_dependency_unavailable` (test_mcp_rs256) | PROVEN `mutation-checked` |

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

**Row 4 note.** `alg` rejection is code-reviewed but there is no offline case
that feeds a forged `alg: none` token through `verify()`. The check is the
first statement in the method, so the risk of silent regression is low, but it
is not tested. Marked CODE_ONLY rather than PROVEN deliberately.

---

## B. Authorization (5–8)

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 5 | Authenticated principal lacks a required scope for the tool | `PERMISSION_DENIED`, distinct from `UNAUTHENTICATED`. | `test:test_insufficient_scope_is_permission_denied` | PROVEN |
| 6 | `work_item.grant` / `work_item.revoke` called with only `work_item.write` | Denied. Delegation administration needs `session_ops.access.manage`. | `test:test_work_item_grant_requires_access_manage` `test:test_work_item_revoke_requires_access_manage` | PROVEN `mutation-checked` |

**Row 3 note.** `aud` is checked twice: once in `validate_registered_claims`
and again in `principal_from_claims`. The cited test exercises
`principal_from_claims`. Deleting the `aud` check there fails it; deleting the
one in `validate_registered_claims` leaves the suite green. The behaviour is
covered once, which is enough for the behaviour, but the row previously named
the wrong layer as the proof.

**Row 6 note.** The grant half was already held. The revoke half was not: the
cited case only ever called `work_item.grant`, so deleting
`session_ops.access.manage` from `TOOL_REQUIRED_SCOPES["work_item.revoke"]`
left all cases green. A symmetric revoke case now exists and goes red on that
deletion.
| 7 | A `write` grantee tries to grant to a third party, or revoke another principal's grant | Denied. `write` never implies grant administration. | `test:test_write_grantee_cannot_grant_or_revoke` | PROVEN |
| 8 | Owner grants to a target that is unknown, disabled, or in another tenant | Refused **before** the grant row is written; cross-tenant attempts leave no row. | `test:test_unknown_target_is_rejected_by_default_directory` `test:test_same_tenant_unregistered_target_is_rejected` `test:test_target_must_exist_be_enabled_and_be_same_tenant` `test:test_cross_tenant_target_is_rejected_before_grant_is_written` | PROVEN |

---

## C. Isolation (9–11)

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 9 | Same-tenant principal with no grant reads another's WorkItem | Denied. | `test:test_same_tenant_stranger_cannot_read` | PROVEN |
| 10 | Another tenant reads, and separately: the WorkItem does not exist | **Indistinguishable.** A caller must not be able to probe for existence. | `test:test_other_tenant_cannot_read` `test:test_missing_cross_tenant_and_no_grant_are_identical` `test:test_cross_tenant_and_missing_resource_are_indistinguishable` `code:NotAuthorized()` (empty message) | PROVEN |
| 11 | A `read` grant tries to update status | Denied; `read` does not imply `write`. | `test:test_read_grant_allows_read_but_not_write` `test:test_write_grant_allows_status_update` | PROVEN |

---

## D. Session linking and F-13 (12–15)

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 12 | Caller constructs its own landing evidence and passes it to `link_session` | Rejected outright **even when a valid unclaimed landing event exists**. `LandingEvidence` no longer exists in the source; any non-`None` `evidence` raises. | `test:test_caller_supplied_evidence_is_rejected_even_with_valid_landing` `test:test_caller_supplied_evidence_is_rejected` `test:test_caller_supplied_evidence_is_rejected_by_authority` `test:test_plain_ids_convref_and_booleans_are_not_evidence_inputs` `code:link_session` (`if evidence is not None: raise`) | PROVEN `mutation-checked` |

**Row 12 note.** The original evidence for this row was
`test_caller_supplied_evidence_is_rejected`, which runs on a session with no
landing event at all — so the *neighbouring* check (`landing is None`)
raised first, and deleting the `evidence is not None` guard left the test
green. The row was PROVEN on a case that could not see its own subject.

The replacement seeds a confirmed, complete, unconsumed event first, asserts the
refusal, asserts the event is still unconsumed and no `session_links` row was
written, and then links the same session without evidence to prove the seeded
event was usable. Mutation check: with the guard deleted, the old case stays
green and only the new one fails.
| 13 | No server-recorded landing event exists for the conversation | `QuarantineNotCleared`. Nothing is written. | `test:test_link_requires_server_recorded_landing` `test:test_unconfirmed_f13_evidence_is_rejected` | PROVEN |
| 14 | A confirmed event is consumed, then replayed | Second attempt fails. Consumption and link creation happen in one transaction. | `test:test_confirmed_complete_landing_can_be_consumed_once` `test:test_event_replay_cannot_create_a_second_link` `code:link_session` (`consumed_at IS NULL` → `UPDATE consumed_at` → `INSERT session_links`) | PROVEN |
| 15 | Landing is `mismatch`, `ambiguous`, or evidence incomplete; or the send returned 409 | No event recorded, no link. Fail closed. | `test:test_mismatch_ambiguous_and_incomplete_landing_fail_closed` `test:test_mismatch_409_does_not_record_or_link` `test:test_ambiguous_multiple_ids_do_not_record_or_link` `test:test_incomplete_reply_does_not_record_or_link` | PROVEN |

---

## E. Atomicity and audit (16–17)

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 16 | A concurrent revoke lands between the authorization check and the mutation; or a relink moves the session between the unlink check and the delete | Both fail closed. Check and mutation share one `BEGIN IMMEDIATE` on one connection. | `test:test_begin_immediate_blocks_second_writer_before_it_reads` `test:test_stale_expected_link_id_does_not_unlink` `test:test_expected_updated_at_is_compare_and_swap` | PROVEN `mutation-checked` for the lock; see note |

**Row 16 note.** Two claims, two different strengths.

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
| 17 | The audit write fails partway through a mutation | The business change rolls back too. No unaudited mutation survives. | `test:test_audit_failure_rolls_back_work_item_and_owner` `test:test_audit_failure_rolls_back_status_mutation` `test:test_all_successful_mutations_are_audited` `test:test_audit_rows_are_append_only` | PROVEN |

**Row 16 note.** The offline cases prove the *precondition* mechanism (a stale
`expected_link_id` or `expected_updated_at` is refused) and read the code to
confirm the transaction shape. They do **not** interleave two real threads and
observe the race. A true concurrency test would need two connections and a
deliberate pause; there is none. What is proven is "the mutation is guarded and
the guard is evaluated inside the same transaction", not "the race was
reproduced".

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

**Row 18 note.** `test_session_context_with_injected_adapter` is the happy path
and does not prove the fail-closed branch. That branch is
`test_context_without_adapter_is_dependency_unavailable`: deleting the
`memory_adapter_command is None` guard turns its expected
`DEPENDENCY_UNAVAILABLE` into `INVALID_ARGUMENT` and it goes red. Checked.

---

## G. Transport and discovery (19–21)

These were absent from v1. An independent review of the deployed surface found
the three holes below; they are not new features, they are repairs to code that
was already running.

| # | Scenario | Expected | Evidence | Status |
|---|---|---|---|---|
| 19 | An unauthenticated caller sends many random `kid` values to amplify JWKS fetches | Fresh-cache misses fail without fetching. Expired-cache refresh is single-flight and backs off after failure. The lock is never held across network I/O. | `test:test_fresh_cache_unknown_kid_never_downloads` `test:test_expired_cache_concurrent_refresh_is_single_flight` `test:test_failed_refresh_is_not_retried_inside_backoff_window` `test:test_refresh_can_retry_after_backoff_expires` (test_mcp_transport) | PROVEN |
| 20 | An attacker-influenced issuer points discovery at an internal address | Redirects refused. HTTPS destinations resolved; any non-global answer rejects the whole URL. | `test:test_non_override_discovery_rejects_different_jwks_origin` `test:test_default_fetcher_does_not_follow_redirects` `test:test_https_jwks_rejects_loopback_link_local_and_private_ips` `test:test_jwks_rejects_all_dns_answers_if_any_is_non_global` (test_mcp_transport) | PROVEN, **partial** |
| 21 | A request targets the MCP endpoint's subpaths | Authenticated. Health and protected-resource metadata stay public. | `test:test_mcp_subpath_is_authenticated` `test:test_mcp_endpoint_itself_is_authenticated` `test:test_health_and_protected_resource_metadata_remain_public` (test_mcp_transport) | PROVEN |

**Row 20 is partial on purpose.** The address check and the HTTP connection
perform *separate* DNS resolutions, so a rebinding attacker can answer
differently the second time. The gap is recorded at the call site and is only
closed by pinning the validated IP to the connection while preserving TLS SNI
and certificate-hostname verification. Do not read row 20 as "SSRF is fixed".

---

## What this matrix does not cover

These are gaps, stated so nobody reads the eighteen rows above as completeness.

| Gap | Why it is not covered | Consequence |
|---|---|---|
| In-process code execution | Python privacy is not a boundary. `_record_trusted_landing` is a plain method; anything running in the SO process can call it and can also write the SQLite file directly. | B1 is closed against a **remote** caller only. Requires deployment isolation. |
| Landing event bound to a WorkItem | `authority_landing_events` stores `session_id` but not `work_item_id`. | If signing succeeds and linking fails, the unconsumed event can be claimed by another WorkItem that holds `write` on that conversation. Bounded, accepted, recorded. |
| DNS rebinding | Rows 20's validation lookup and the client's connection lookup are separate resolutions. | The SSRF defense is real but incomplete. Requires IP pinning. Recorded at the call site. |
| Mutation coverage is not uniform | Five rows are `mutation-checked`; the rest are not. | Treat every unmarked PROVEN as "asserts the behaviour", not "proven necessary" — that is exactly how row 12 stayed PROVEN for three commits. |
| Security tests can skip to green | `test_mcp_rs256` skips as a unit without `cryptography`. The unit job now installs `requirements-mcp.txt` and fails if the rs256 skip reason appears anyway. | Before that guard, the whole signature path could be skipped and the log still read green. Any future optional dependency must get the same treatment. |
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

The two lines that still read "all green" are the residue, and they are not the
same kind of thing. The `aud` one is a redundant second layer with the behaviour
covered elsewhere. The `rowcount` one is unreachable code: no test can fail when
it is deleted, so listing it as evidence would be a lie in the other direction.
