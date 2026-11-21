"""Regression tests for AID-2026-011 conversation reconciliation."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "chatgpt_send.py")

SPEC = importlib.util.spec_from_file_location(
    "chatgpt_send_stage3_cli_test_module", SCRIPT
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


# 跟随生产常量，避免每次扩账号都要改测试（DECISION-147）。
ACCOUNTS = tuple(MODULE.ROTATION_PRIMARY)


class FakeAPI:
    """Minimal substitute for the production ChatGPT API."""

    def __init__(self, account, responses):
        self.account = account
        self.quota = {"remaining": 10}
        self.responses = responses

    def list_conversations(self):
        return self.responses[self.account]


def make_responses(items=None, failures=None):
    items = items or {}
    failures = failures or {}
    responses = {}

    for account in ACCOUNTS + ("nologin",):
        if account in failures:
            responses[account] = failures[account]
        else:
            responses[account] = (
                items.get(account, []),
                200,
                "",
            )

    return responses


def complete_results(items_by_account=None):
    items_by_account = items_by_account or {}
    return {
        account: {
            "status": 200,
            "error": "",
            "items": items_by_account.get(account, []),
            "state": "complete",
        }
        for account in ACCOUNTS
    }


class AID2026011ReconcileTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.registry_path = os.path.join(
            self.tempdir.name,
            "conv-accounts.json",
        )

        self.registry_patch = mock.patch.object(
            MODULE,
            "CONV_MAP_FILE",
            self.registry_path,
        )
        self.registry_patch.start()

        self.addCleanup(self.registry_patch.stop)
        self.addCleanup(self.tempdir.cleanup)

    def registry_bytes(self):
        if not os.path.exists(self.registry_path):
            return None
        with open(self.registry_path, "rb") as registry_file:
            return registry_file.read()

    def write_registry(self, value):
        encoded = json.dumps(
            value,
            ensure_ascii=False,
        ).encode("utf-8")
        with open(self.registry_path, "wb") as registry_file:
            registry_file.write(encoded)
        return encoded

    def install_api(self, responses, constructed=None):
        def factory(account):
            if constructed is not None:
                constructed.append(account)
            return FakeAPI(account, responses)

        return mock.patch.object(MODULE, "ChatGPTAPI", factory)

    def run_main(self, args, responses, constructed=None):
        argv = ["chatgpt_send.py"] + list(args)
        output = io.StringIO()

        with (
            self.install_api(responses, constructed),
            mock.patch.object(MODULE.sys, "argv", argv),
            contextlib.redirect_stdout(output),
        ):
            result = MODULE.main()

        stdout = output.getvalue()
        parsed = json.loads(stdout)
        return result, parsed, stdout

    def reconcile(self, results, registry=None, accounts=ACCOUNTS):
        return MODULE.reconcile_conversation_ownership(
            results,
            registry if registry is not None else {},
            accounts,
        )

    # 1. Frozen test: account 3 and account 4 are enumerated.

    def test_list_all_includes_acct3_and_acct4(self):
        constructed = []

        self.run_main(
            ["--list-all"],
            make_responses(),
            constructed,
        )

        self.assertIn("acct3", constructed)
        self.assertIn("acct4", constructed)

    # 2. Frozen test: unique account 3 conversation.

    def test_reconcile_unique_acct3_conversation(self):
        results = complete_results({
            "acct3": [{"id": "conv-acct3"}],
        })

        result = self.reconcile(results)

        self.assertTrue(result["complete"])
        self.assertEqual(
            result["observed_not_registered"],
            [{"id": "conv-acct3", "account": "acct3"}],
        )
        self.assertEqual(
            result["eligible_registrations"],
            [{"id": "conv-acct3", "account": "acct3"}],
        )

    # 3. Frozen test: unique account 4 conversation.

    def test_reconcile_unique_acct4_conversation(self):
        results = complete_results({
            "acct4": [{"id": "conv-acct4"}],
        })

        result = self.reconcile(results)

        self.assertTrue(result["complete"])
        self.assertEqual(
            result["eligible_registrations"],
            [{"id": "conv-acct4", "account": "acct4"}],
        )

    # 4. Frozen test: duplicate observations are conflicts.

    def test_reconcile_detects_duplicate_conversation_ids(self):
        results = complete_results({
            "acct1": [{"id": "duplicate-id"}],
            "acct2": [{"id": "duplicate-id"}],
        })

        result = self.reconcile(results)

        self.assertEqual(
            result["duplicates"],
            [{
                "id": "duplicate-id",
                "accounts": ["acct1", "acct2"],
                "status": "duplicate_observation",
            }],
        )
        self.assertEqual(result["eligible_registrations"], [])

    # 5. Frozen test: existing registry owner is not overwritten.

    def test_reconcile_does_not_overwrite_registry_owner(self):
        results = complete_results({
            "acct1": [{"id": "existing-id"}],
        })

        result = self.reconcile(results, {
            "existing-id": {"account": "acct2"},
        })

        self.assertEqual(result["eligible_registrations"], [])
        self.assertEqual(len(result["registry_conflicts"]), 1)
        self.assertEqual(
            result["registry_conflicts"][0]["registry_account"],
            "acct2",
        )

    # 6. Frozen test: incomplete enumeration cannot claim uniqueness.

    def test_reconcile_incomplete_enumeration_does_not_claim_uniqueness(
        self,
    ):
        results = complete_results({
            "acct1": [{"id": "possibly-duplicate"}],
        })
        results["acct2"] = {
            "status": 429,
            "error": "rate limited",
            "items": [],
            "state": "failed",
        }

        result = self.reconcile(results)

        self.assertFalse(result["complete"])
        self.assertEqual(result["eligible_registrations"], [])
        self.assertEqual(
            result["enumeration"]["acct2"]["state"],
            "failed",
        )

    # 7. Frozen test: absence does not establish ownership.

    def test_reconcile_does_not_infer_owner_from_absence(self):
        results = complete_results({
            "acct1": [{"id": "observed-only-once"}],
        })

        result = self.reconcile(results)

        self.assertEqual(
            result["observed_not_registered"],
            [{"id": "observed-only-once", "account": "acct1"}],
        )
        self.assertEqual(result["registry_conflicts"], [])

    # 8. Frozen test: external IDs are validated through safe_id.

    def test_reconcile_uses_safe_id_for_external_ids(self):
        results = complete_results({
            "acct1": [
                {"id": "valid-external-id"},
                {"id": "../invalid"},
                {"id": None},
            ],
        })

        result = self.reconcile(results)

        self.assertEqual(result["invalid_ids"], 2)
        self.assertEqual(
            result["observed_not_registered"],
            [{"id": "valid-external-id", "account": "acct1"}],
        )

    # 9. This test exercises record_conv's atomic update path,
    # not the --list-all reconciliation wiring.

    def test_record_conv_uses_atomic_locked_update_path(self):
        original_update = MODULE._update_json
        calls = []

        def tracked_update(*args, **kwargs):
            calls.append(args[0])
            return original_update(*args, **kwargs)

        with mock.patch.object(
            MODULE,
            "_update_json",
            tracked_update,
        ):
            MODULE.record_conv(
                "atomic-test-id",
                "acct1",
                None,
            )

        self.assertTrue(calls)
        self.assertEqual(calls[0], self.registry_path)

        with open(self.registry_path, "r", encoding="utf-8") as f:
            registry = json.load(f)

        self.assertEqual(
            registry["atomic-test-id"]["account"],
            "acct1",
        )

    # 10. Frozen test: original output compatibility is preserved.

    def test_list_all_retains_existing_output_compatibility(self):
        _, output, _ = self.run_main(
            ["--list-all"],
            make_responses(),
        )

        self.assertIn("conversations", output)
        self.assertIn("by_account", output)
        self.assertIn("summary", output)
        self.assertIn("quota", output)
        self.assertIn("errors", output)
        self.assertIn("enumeration", output)
        self.assertIn("reconciliation", output)

    # 11. Frozen test: rotation cannot transfer an existing owner.

    def test_rotation_does_not_change_existing_conversation_owner(self):
        original = {
            "rotated-id": {
                "account": "acct1",
                "owner": "acct1",
                "prompt": "original",
                "ts": "2026-01-01T00:00:00",
            }
        }
        self.write_registry(original)

        MODULE.record_conv(
            "rotated-id",
            "acct1",
            "rotated-prompt",
        )

        with open(self.registry_path, "r", encoding="utf-8") as f:
            actual = json.load(f)

        self.assertEqual(actual["rotated-id"]["account"], "acct1")
        self.assertEqual(actual["rotated-id"]["owner"], "acct1")

    # 12. Q1 and D2: dry-run never writes; apply writes through
    # record_conv and retains the existing 500-record prune policy.

    def test_list_all_dry_run_never_writes_and_apply_writes(self):
        initial = {
            "old-%03d" % index: {
                "account": "acct1",
                "prompt": None,
                "ts": "2020-01-%02dT00:00:00" % (index % 28 + 1),
            }
            for index in range(493)
        }

        items = {
            "acct1": [
                {"id": "bulk-acct1-%03d" % index}
                for index in range(549)
            ],
            "acct2": [
                {"id": "bulk-acct2-%03d" % index}
                for index in range(116)
            ],
            "acct3": [
                {"id": "bulk-acct3-%03d" % index}
                for index in range(10)
            ],
            "acct4": [
                {"id": "bulk-acct4-%03d" % index}
                for index in range(8)
            ],
        }
        responses = make_responses(items)

        original_bytes = self.write_registry(initial)

        self.run_main(
            ["--list-all"],
            responses,
        )

        # Dry-run must leave the existing file byte-for-byte unchanged.
        self.assertEqual(self.registry_bytes(), original_bytes)

        self.run_main(
            ["--list-all", "--apply"],
            responses,
        )

        with open(self.registry_path, "r", encoding="utf-8") as f:
            after_apply = json.load(f)

        # Every pre-existing record survives the bulk registration.
        for conv_id, record in initial.items():
            self.assertEqual(after_apply.get(conv_id), record)

        # All 683 new IDs are added; no 500-record truncation occurs.
        self.assertEqual(len(after_apply), 493 + 683)

        for account, count in (
            ("acct1", 549),
            ("acct2", 116),
            ("acct3", 10),
            ("acct4", 8),
        ):
            for index in range(count):
                conv_id = "bulk-%s-%03d" % (account, index)
                self.assertEqual(
                    after_apply[conv_id]["account"],
                    account,
                )
                self.assertIsNone(after_apply[conv_id]["prompt"])

    # 13. Q2: HTTP 429 stops subsequent enumeration and all registration.

    def test_list_all_429_marks_later_accounts_not_attempted_and_writes_nothing(
        self,
    ):
        constructed = []
        initial = {"existing": {"account": "acct1"}}
        original_bytes = self.write_registry(initial)

        responses = make_responses(
            failures={
                "acct2": ([], 429, "rate limited"),
            }
        )

        _, output, _ = self.run_main(
            ["--list-all", "--apply"],
            responses,
            constructed,
        )

        enumeration = output["enumeration"]

        self.assertEqual(enumeration["acct2"]["state"], "failed")
        self.assertEqual(
            enumeration["acct3"]["state"],
            "not_attempted",
        )
        self.assertEqual(
            enumeration["acct4"]["state"],
            "not_attempted",
        )
        self.assertFalse(output["reconciliation"]["complete"])
        self.assertEqual(
            output["reconciliation"]["registered"],
            [],
        )
        self.assertEqual(constructed, ["acct1", "acct2"])
        self.assertEqual(self.registry_bytes(), original_bytes)

    # 14. Q3: nologin is excluded by default and included only on opt-in.

    def test_list_all_nologin_default_exclusion_and_opt_in(self):
        default_constructed = []

        self.run_main(
            ["--list-all"],
            make_responses(),
            default_constructed,
        )

        self.assertNotIn("nologin", default_constructed)

        opt_in_constructed = []

        self.run_main(
            ["--list-all", "--include-nologin"],
            make_responses(),
            opt_in_constructed,
        )

        self.assertIn("nologin", opt_in_constructed)

    # 15. FACT-4: an account-valued owner conflicting with account
    # is a conflict, and record_conv refuses to overwrite the record.

    def test_reconcile_account_valued_owner_conflict(self):
        results = complete_results({
            "acct1": [{"id": "owner-conflict-id"}],
        })

        result = self.reconcile(results, {
            "owner-conflict-id": {
                "account": "acct1",
                "owner": "acct2",
                "prompt": "preserve",
                "ts": "2026-01-01",
            },
        })

        self.assertEqual(
            result["registry_conflicts"],
            [{
                "id": "owner-conflict-id",
                "observed_accounts": ["acct1"],
                "registry_account": "acct1",
                "reason": (
                    "registry owner disagrees with registry account"
                ),
            }],
        )
        self.assertEqual(result["eligible_registrations"], [])

        original = {
            "owner-conflict-id": {
                "account": "acct1",
                "owner": "acct2",
                "prompt": "preserve",
                "ts": "2026-01-01",
            }
        }
        original_bytes = self.write_registry(original)

        MODULE.record_conv(
            "owner-conflict-id",
            "acct1",
            "must-not-overwrite",
        )

        self.assertEqual(self.registry_bytes(), original_bytes)


    def test_reconcile_excludes_nologin_from_registered_not_observed(self):
        results = complete_results()

        result = self.reconcile(results, {
            "anonymous-dict": {"account": "nologin"},
            "anonymous-legacy": "nologin",
            "ordinary-record": {"account": "acct1"},
        })

        self.assertEqual(
            result["registered_not_observed"],
            [{"id": "ordinary-record", "account": "acct1"}],
        )

    def test_reconcile_reports_malformed_unobserved_records_separately(self):
        results = complete_results()

        result = self.reconcile(results, {
            "valid-record": {"account": "acct1"},
            "null-account": {"account": None},
            "non-dictionary": ["acct2"],
        })

        self.assertEqual(
            result["registered_not_observed"],
            [{"id": "valid-record", "account": "acct1"}],
        )
        self.assertEqual(
            result["malformed_registry_records"],
            [
                {
                    "id": "null-account",
                    "reason": "registry record account is missing or invalid",
                },
                {
                    "id": "non-dictionary",
                    "reason": (
                        "registry record is neither a dictionary "
                        "nor a legacy account string"
                    ),
                },
            ],
        )
