"""End-to-end tests against a real Memory Adapter process.

These tests are skipped unless SO_MEMORY_ADAPTER_COMMAND is set, for example:

  SO_MEMORY_ADAPTER_COMMAND='["wsl.exe","-e","/path/memory-adapter","--db","/path/memory.sqlite3"]'

The value must be a JSON array of argv strings (a plain space-separated
string is also accepted). No mock stands in for the adapter here.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.session_ops.memory_adapter_client import (  # noqa: E402
    MemoryAdapterClient,
    MemoryProvenance,
)

COMMAND_ENV = "SO_MEMORY_ADAPTER_COMMAND"


def adapter_command() -> list[str] | None:
    raw = os.environ.get(COMMAND_ENV, "").strip()
    if not raw:
        return None
    if raw.startswith("["):
        parsed = json.loads(raw)
        return [str(part) for part in parsed]
    return shlex.split(raw, posix=(os.name != "nt"))


SKIP_REASON = (
    "%s is not set; a real Memory Adapter executable is required for "
    "end-to-end verification" % COMMAND_ENV
)


@unittest.skipUnless(adapter_command(), SKIP_REASON)
class MemoryAdapterEndToEndTestCase(unittest.TestCase):
    def setUp(self) -> None:
        command = adapter_command()
        assert command is not None
        self.client = MemoryAdapterClient(command, timeout_seconds=30.0)
        self.work_item_id = "wi_" + str(uuid.uuid4())
        self.marker = "e2e-" + str(uuid.uuid4())

    def provenance(self, session_id: str) -> MemoryProvenance:
        return MemoryProvenance(
            source_type="execution_result",
            source_id=session_id,
            captured_at="2026-11-21T10:00:00Z",
        )

    def test_store_then_retrieve_roundtrip(self) -> None:
        stored = self.client.store(
            work_item_id=self.work_item_id,
            session_id="session-e2e-1",
            content="%s migration completed" % self.marker,
            provenance=self.provenance("session-e2e-1"),
        )
        self.assertTrue(stored.memory_id)
        self.assertEqual(stored.work_item_id, self.work_item_id)

        result = self.client.retrieve(
            work_item_id=self.work_item_id,
            query="migration completed",
        )
        self.assertTrue(result.entries, "stored memory was not retrieved")
        for entry in result.entries:
            self.assertEqual(entry.work_item_id, self.work_item_id)
        matched = [e for e in result.entries if self.marker in e.content]
        self.assertTrue(matched, "marker content missing from retrieval")
        self.assertEqual(
            matched[0].provenance.get("source_id"), "session-e2e-1"
        )
        self.assertEqual(
            matched[0].provenance.get("captured_at"),
            "2026-11-21T10:00:00Z",
        )

    def test_cross_session_retrieval_within_one_work_item(self) -> None:
        self.client.store(
            work_item_id=self.work_item_id,
            session_id="session-e2e-a",
            content="%s written in session A" % self.marker,
            provenance=self.provenance("session-e2e-a"),
        )
        self.client.store(
            work_item_id=self.work_item_id,
            session_id="session-e2e-b",
            content="%s written in session B" % self.marker,
            provenance=self.provenance("session-e2e-b"),
        )
        cross = self.client.retrieve(
            work_item_id=self.work_item_id, query=self.marker
        )
        sessions = {e.session_id for e in cross.entries}
        self.assertEqual(
            sessions, {"session-e2e-a", "session-e2e-b"}
        )

        scoped = self.client.retrieve(
            work_item_id=self.work_item_id,
            query=self.marker,
            session_id="session-e2e-a",
        )
        self.assertEqual(
            {e.session_id for e in scoped.entries}, {"session-e2e-a"}
        )

    def test_cross_work_item_isolation(self) -> None:
        other_work_item_id = "wi_" + str(uuid.uuid4())
        self.client.store(
            work_item_id=other_work_item_id,
            session_id="session-e2e-other",
            content="%s belongs elsewhere" % self.marker,
            provenance=self.provenance("session-e2e-other"),
        )
        result = self.client.retrieve(
            work_item_id=self.work_item_id, query=self.marker
        )
        self.assertEqual(result.entries, ())

    def test_empty_retrieval_is_success(self) -> None:
        result = self.client.retrieve(
            work_item_id="wi_" + str(uuid.uuid4()),
            query="a query that matches nothing at all",
        )
        self.assertEqual(result.entries, ())


if __name__ == "__main__":
    unittest.main()
