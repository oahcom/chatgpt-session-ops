"""Protocol guard tests for the Memory Adapter client (issue #112).

Each test pins one `raise` in ``src/session_ops/memory_adapter_client.py``
that is otherwise unreachable by the existing suite: deleting it (replacing
it with ``pass``) leaves every other test green. Every assertion checks both
the error code and a distinctive message fragment, so a different guard
firing cannot satisfy the test.

The fake adapter reads one request line, echoes its ``request_id`` (without
that echo the client rejects the response before reaching the target guard)
and prints one malformed response chosen by ``sys.argv[1]``.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.session_ops.memory_adapter_client import (  # noqa: E402
    MemoryAdapterClient,
    MemoryAdapterError,
    MemoryProvenance,
)

FAKE_ADAPTER = r'''
import json, sys

request = json.loads(sys.stdin.readline() or "{}")
request_id = request.get("request_id", "")
requested_work_item_id = request.get("payload", {}).get("work_item_id", "")
scenario = sys.argv[1]
base = {"contract_version": "0.1", "request_id": request_id}

if scenario == "no_result":
    response = dict(base, ok=True)
elif scenario == "no_error_object":
    response = dict(base, ok=False)
elif scenario == "no_entries":
    response = dict(base, ok=True, result={})
elif scenario == "non_object_entry":
    response = dict(base, ok=True, result={"entries": ["not-an-object"]})
elif scenario == "scope_violation":
    response = dict(base, ok=True, result={"entries": [{
        "memory_id": "mem-1",
        "work_item_id": "wi_someone-elses-work-item",
        "session_id": "session-002",
        "content": "out of scope",
        "provenance": {},
        "created_at": "2026-11-21T10:00:00Z",
        "score": 0.8,
    }]})
elif scenario == "entry_missing_key":
    response = dict(base, ok=True, result={"entries": [{
        "work_item_id": requested_work_item_id,
        "session_id": "session-001",
        "content": "in scope but incomplete",
        "provenance": {},
        "created_at": "2026-11-21T10:00:00Z",
        "score": 0.8,
    }]})
elif scenario == "entry_bad_score":
    response = dict(base, ok=True, result={"entries": [{
        "memory_id": "mem-1",
        "work_item_id": requested_work_item_id,
        "session_id": "session-001",
        "content": "in scope but unparseable score",
        "provenance": {},
        "created_at": "2026-11-21T10:00:00Z",
        "score": "not-a-number",
    }]})
else:
    sys.exit(2)

sys.stdout.write(json.dumps(response) + "\n")
'''


class ProtocolGuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="so-ma-guard-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.adapter = Path(self.tmpdir) / "fake_adapter.py"
        self.adapter.write_text(FAKE_ADAPTER, encoding="utf-8")
        self.work_item_id = "wi_" + str(uuid.uuid4())

    def client(self, scenario: str) -> MemoryAdapterClient:
        return MemoryAdapterClient(
            [sys.executable, str(self.adapter), scenario]
        )

    def store(self, client: MemoryAdapterClient) -> None:
        client.store(
            work_item_id=self.work_item_id,
            session_id="session-001",
            content="content",
            provenance=MemoryProvenance(
                source_type="conversation",
                source_id="session-001",
                captured_at="2026-11-21T10:00:00Z",
            ),
        )

    def retrieve(self, client: MemoryAdapterClient) -> None:
        client.retrieve(work_item_id=self.work_item_id, query="migration")

    def test_oserror_on_spawn_maps_to_adapter_unavailable(self) -> None:
        # Guard: except OSError around subprocess.run
        client = self.client("no_result")
        with mock.patch(
            "src.session_ops.memory_adapter_client.subprocess.run",
            side_effect=OSError("boom"),
        ):
            with self.assertRaises(MemoryAdapterError) as caught:
                self.store(client)
        self.assertEqual(caught.exception.code, "ADAPTER_UNAVAILABLE")
        self.assertIn("could not be started", caught.exception.message)

    def test_success_response_without_result_object_is_rejected(self) -> None:
        # Guard: ok is True but result is not a dict
        with self.assertRaises(MemoryAdapterError) as caught:
            self.store(self.client("no_result"))
        self.assertEqual(caught.exception.code, "SERIALIZATION_ERROR")
        self.assertIn("has no result object", caught.exception.message)

    def test_failure_response_without_error_object_is_rejected(self) -> None:
        # Guard: ok is False but error is not a dict
        with self.assertRaises(MemoryAdapterError) as caught:
            self.store(self.client("no_error_object"))
        self.assertEqual(caught.exception.code, "SERIALIZATION_ERROR")
        self.assertIn("without an error object", caught.exception.message)

    def test_retrieve_without_entries_list_is_rejected(self) -> None:
        # Guard: result.entries is not a list
        with self.assertRaises(MemoryAdapterError) as caught:
            self.retrieve(self.client("no_entries"))
        self.assertEqual(caught.exception.code, "SERIALIZATION_ERROR")
        self.assertIn("has no entries list", caught.exception.message)

    def test_retrieve_non_object_entry_is_rejected(self) -> None:
        # Guard: an entry is not a dict
        with self.assertRaises(MemoryAdapterError) as caught:
            self.retrieve(self.client("non_object_entry"))
        self.assertEqual(caught.exception.code, "SERIALIZATION_ERROR")
        self.assertIn("non-object entry", caught.exception.message)

    def test_retrieve_entry_outside_scope_is_rejected(self) -> None:
        # Guard: entry work_item_id differs from the requested one
        with self.assertRaises(MemoryAdapterError) as caught:
            self.retrieve(self.client("scope_violation"))
        self.assertEqual(caught.exception.code, "SCOPE_VIOLATION")
        self.assertIn("outside the requested", caught.exception.message)

    def test_retrieve_malformed_entry_is_rejected(self) -> None:
        # Guard: except (KeyError, TypeError, ValueError) around RetrievedMemory
        for scenario in ("entry_missing_key", "entry_bad_score"):
            with self.subTest(scenario=scenario):
                with self.assertRaises(MemoryAdapterError) as caught:
                    self.retrieve(self.client(scenario))
                self.assertEqual(
                    caught.exception.code, "SERIALIZATION_ERROR"
                )
                self.assertIn("entry is malformed", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
