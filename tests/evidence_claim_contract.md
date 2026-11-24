# Evidence Claim Contract Maintenance

`evidence_claim_contract.json` is the independent
source of truth for the expected number of documented
test_hardening.py regression-count declarations.

Current expected declaration count: 6.

## Maintenance rules

1. Do not derive `expected_declaration_count`
   dynamically from SKILL.md or references/.

2. Any intentional addition or removal of a regression-count
   declaration must update this contract in the same change.

3. The change must explain why the declaration was added
   or removed and identify the affected document and pattern.

4. Unintentional declaration loss or addition must be fixed
   in the document, not accommodated by changing the contract.

5. Changes to this contract require independent review.
   The contract must not be automatically regenerated
   during test execution.

## Scope

This contract covers only regression-count declarations
for tests/test_hardening.py.

It does not govern repository-wide pytest collection,
full-suite pass/skip counts, or subtest counts.
