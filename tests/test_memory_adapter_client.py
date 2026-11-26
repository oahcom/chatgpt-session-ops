"""Tests for the Memory Adapter client (T154-19 assertions 13-26).

The fake adapter is a Python script invoked as argv. It validates client
parsing and error mapping; it is not a Memory Adapter implementation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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
    KNOWN_ERROR_CODES,
    MemoryAdapterClient,
    MemoryAdapterError,
    MemoryProvenance,
)

FAKE_ADAPTER = r'''
import json, os, sys, time

scenario = os.environ.get("FAKE_SCENARIO", "store_ok")
record = os.environ.get("FAKE_RECORD")

raw = sys.stdin.readline()
if record:
    with open(record, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": sys.argv, "raw": raw}) + "\n")

try:
    request = json.loads(raw)
except Exception:
    request = {}

request_id = request.get("request_id", "")
payload = request.get("payload") or {}

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def ok(result):
    return {
        "contract_version": "0.1",
        "request_id": request_id,
        "ok": True,
        "result": result,
    }

def fail(code, message="failure"):
    return {
        "contract_version": "0.1",
        "request_id": request_id,
        "ok": False,
        "error": {"code": code, "message": message, "retryable": False},
    }

if scenario == "store_ok":
    emit(ok({"memory_id": "mem-" + request_id[:8]}))
elif scenario == "store_no_memory_id":
    emit(ok({}))
elif scenario == "retrieve_empty":
    emit(ok({"entries": [], "entries_included": 0, "entries_omitted": 0}))
elif scenario == "retrieve_one":
    emit(ok({"entries": [{
        "memory_id": "mem-1",
        "work_item_id": payload.get("work_item_id"),
        "session_id": "session-001",
        "content": payload.get("query") or "stored content",
        "provenance": {
            "source_type": "execution_result",
            "source_id": "session-001",
            "captured_at": "2026-11-21T10:00:00Z",
        },
        "created_at": "2026-11-21T10:00:00Z",
        "score": 0.85,
    }], "entries_included": 1, "entries_omitted": 0}))
elif scenario == "retrieve_scope_violation":
    emit(ok({"entries": [
        {
            "memory_id": "mem-1",
            "work_item_id": payload.get("work_item_id"),
            "session_id": "session-001",
            "content": "in scope",
            "provenance": {},
            "created_at": "2026-11-21T10:00:00Z",
            "score": 0.9,
        },
        {
            "memory_id": "mem-2",
            "work_item_id": "wi_00000000-0000-4000-8000-000000000000",
            "session_id": "session-002",
            "content": "out of scope",
            "provenance": {},
            "created_at": "2026-11-21T10:00:00Z",
            "score": 0.8,
        },
    ]}))
elif scenario == "malformed_json":
    sys.stdout.write("this is not json\n")
elif scenario == "two_lines":
    sys.stdout.write(json.dumps(ok({"memory_id": "mem-1"})) + "\n")
    sys.stdout.write(json.dumps(ok({"memory_id": "mem-2"})) + "\n")
elif scenario == "json_array":
    sys.stdout.write(json.dumps([1, 2, 3]) + "\n")
elif scenario == "empty_stdout":
    sys.stderr.write("diagnostic only\n")
elif scenario == "stderr_json":
    sys.stderr.write(json.dumps(ok({"memory_id": "mem-1"})) + "\n")
elif scenario == "request_id_mismatch":
    response = ok({"memory_id": "mem-1"})
    response["request_id"] = "00000000-0000-4000-8000-000000000000"
    emit(response)
elif scenario == "bad_version":
    response = ok({"memory_id": "mem-1"})
    response["contract_version"] = "9.9"
    emit(response)
elif scenario == "crash":
    sys.stderr.write("boom\n")
    sys.exit(3)
elif scenario == "timeout":
    time.sleep(5)
    emit(ok({"memory_id": "mem-1"}))
elif scenario.startswith("error_"):
    emit(fail(scenario[len("error_"):]))
elif scenario == "unknown_error_code":
    emit(fail("SOMETHING_ELSE"))
elif scenario == "echo":
    emit(ok({
        "memory_id": "mem-echo",
        "entries": [],
        "echo_payload": payload,
    }))
else:
    emit(fail("INTERNAL_ERROR", "unhandled scenario " + scenario))
'''


class MemoryAdapterClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="so-ma-client-")
        self.adapter = Path(self.tmpdir) / "fake_adapter.py"
        self.adapter.write_text(FAKE_ADAPTER, encoding="utf-8")
        self.record = Path(self.tmpdir) / "invocations.jsonl"
        self.work_item_id = "wi_" + str(uuid.uuid4())
        self._saved = {
            "FAKE_SCENARIO": os.environ.get("FAKE_SCENARIO"),
            "FAKE_RECORD": os.environ.get("FAKE_RECORD"),
        }
        os.environ["FAKE_RECORD"] = str(self.record)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def client(self, scenario: str, **kwargs) -> MemoryAdapterClient:
        os.environ["FAKE_SCENARIO"] = scenario
        return MemoryAdapterClient(
            [sys.executable, str(self.adapter)],
            **kwargs,
        )

    def provenance(self) -> MemoryProvenance:
        return MemoryProvenance(
            source_type="conversation",
            source_id="session-001",
            captured_at="2026-11-21T10:00:00Z",
        )

    def invocations(self) -> list[dict]:
        if not self.record.exists():
            return []
        return [
            json.loads(line)
            for line in self.record.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class InvocationTestCase(MemoryAdapterClientTestCase):
    def test_command_is_invoked_as_argv_without_shell(self) -> None:
        # Assertion 13
        os.environ["FAKE_SCENARIO"] = "store_ok"
        metachar = "weird;arg && echo PWNED $HOME | tee"
        command = [sys.executable, str(self.adapter), metachar]
        client = MemoryAdapterClient(command)
        client.store(
            work_item_id=self.work_item_id,
            session_id="session-001",
            content="content; rm -rf / && echo $PATH",
            provenance=self.provenance(),
        )
        calls = self.invocations()
        self.assertEqual(len(calls), 1)
        # sys.argv[0] of a script run as "python script" is the script path,
        # so the recorded argv is the command minus the interpreter. Every
        # element must arrive intact: no word splitting, no expansion.
        self.assertEqual(calls[0]["argv"], command[1:])
        self.assertIn(metachar, calls[0]["argv"])
        self.assertFalse(self.record.with_suffix(".pwned").exists())

    def test_request_is_single_json_line_terminated_by_eof(self) -> None:
        # Assertion 14
        client = self.client("store_ok")
        client.store(
            work_item_id=self.work_item_id,
            session_id="session-001",
            content="line one\nline two",
            provenance=self.provenance(),
            metadata={"k": "v"},
        )
        calls = self.invocations()
        self.assertEqual(len(calls), 1)
        raw = calls[0]["raw"]
        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(raw.count("\n"), 1)
        request = json.loads(raw)
        self.assertEqual(request["contract_version"], "0.1")
        self.assertEqual(request["operation"], "store")
        self.assertEqual(request["payload"]["work_item_id"], self.work_item_id)
        self.assertEqual(
            request["payload"]["session_id"], "session-001"
        )
        self.assertEqual(
            request["payload"]["content"], "line one\nline two"
        )
        self.assertEqual(
            request["payload"]["provenance"],
            {
                "source_type": "conversation",
                "source_id": "session-001",
                "captured_at": "2026-11-21T10:00:00Z",
            },
        )
        self.assertEqual(request["payload"]["metadata"], {"k": "v"})
        self.assertTrue(request["request_id"])

    def test_retrieve_request_shape_and_defaults(self) -> None:
        client = self.client("retrieve_empty")
        client.retrieve(work_item_id=self.work_item_id, query="migration")
        request = json.loads(self.invocations()[0]["raw"])
        self.assertEqual(request["operation"], "retrieve")
        self.assertEqual(
            request["payload"],
            {
                "work_item_id": self.work_item_id,
                "query": "migration",
                "session_id": None,
                "limit": 10,
                "threshold": 0.34,
            },
        )

    def test_metadata_defaults_to_empty_object(self) -> None:
        client = self.client("store_ok")
        client.store(
            work_item_id=self.work_item_id,
            session_id="session-001",
            content="c",
            provenance=self.provenance(),
        )
        request = json.loads(self.invocations()[0]["raw"])
        self.assertEqual(request["payload"]["metadata"], {})


class ResponseParsingTestCase(MemoryAdapterClientTestCase):
    def test_stdout_is_protocol_only(self) -> None:
        # Assertion 15
        client = self.client("store_ok")
        stored = client.store(
            work_item_id=self.work_item_id,
            session_id="session-001",
            content="c",
            provenance=self.provenance(),
        )
        self.assertTrue(stored.memory_id.startswith("mem-"))
        self.assertEqual(stored.work_item_id, self.work_item_id)
        self.assertEqual(stored.session_id, "session-001")

    def test_stderr_is_not_parsed_as_protocol(self) -> None:
        # Assertion 16
        client = self.client("empty_stdout")
        with self.assertRaises(MemoryAdapterError) as ctx:
            client.store(
                work_item_id=self.work_item_id,
                session_id="session-001",
                content="c",
                provenance=self.provenance(),
            )
        self.assertEqual(ctx.exception.code, "SERIALIZATION_ERROR")

        stderr_only = self.client("stderr_json")
        with self.assertRaises(MemoryAdapterError) as ctx2:
            stderr_only.store(
                work_item_id=self.work_item_id,
                session_id="session-001",
                content="c",
                provenance=self.provenance(),
            )
        self.assertEqual(ctx2.exception.code, "SERIALIZATION_ERROR")

    def test_request_id_is_correlated(self) -> None:
        # Assertion 17
        client = self.client("store_ok")
        client.store(
            work_item_id=self.work_item_id,
            session_id="session-001",
            content="first",
            provenance=self.provenance(),
        )
        client.store(
            work_item_id=self.work_item_id,
            session_id="session-001",
            content="second",
            provenance=self.provenance(),
        )
        ids = [
            json.loads(call["raw"])["request_id"]
            for call in self.invocations()
        ]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])
        for request_id in ids:
            self.assertEqual(len(request_id), 36)
            self.assertEqual(str(uuid.UUID(request_id)), request_id)

        mismatch = self.client("request_id_mismatch")
        with self.assertRaises(MemoryAdapterError) as ctx:
            mismatch.store(
                work_item_id=self.work_item_id,
                session_id="session-001",
                content="c",
                provenance=self.provenance(),
            )
        self.assertEqual(ctx.exception.code, "SERIALIZATION_ERROR")
        self.assertIsNotNone(ctx.exception.request_id)

    def test_invalid_responses_are_rejected(self) -> None:
        # Assertion 18
        cases = {
            "malformed_json": "SERIALIZATION_ERROR",
            "two_lines": "SERIALIZATION_ERROR",
            "json_array": "SERIALIZATION_ERROR",
            "empty_stdout": "SERIALIZATION_ERROR",
            "stderr_json": "SERIALIZATION_ERROR",
            "request_id_mismatch": "SERIALIZATION_ERROR",
            "store_no_memory_id": "SERIALIZATION_ERROR",
            "bad_version": "UNSUPPORTED_VERSION",
        }
        for scenario, expected in cases.items():
            with self.subTest(scenario=scenario):
                client = self.client(scenario)
                with self.assertRaises(MemoryAdapterError) as ctx:
                    client.store(
                        work_item_id=self.work_item_id,
                        session_id="session-001",
                        content="c",
                        provenance=self.provenance(),
                    )
                self.assertEqual(ctx.exception.code, expected)

    def test_every_retrieved_work_item_id_is_checked(self) -> None:
        # Assertion 19
        clean = self.client("retrieve_one")
        result = clean.retrieve(
            work_item_id=self.work_item_id, query="migration"
        )
        self.assertEqual(len(result.entries), 1)
        entry = result.entries[0]
        self.assertEqual(entry.work_item_id, self.work_item_id)
        self.assertEqual(entry.score, 0.85)
        self.assertEqual(
            entry.provenance["source_type"], "execution_result"
        )
        self.assertEqual(entry.created_at, "2026-11-21T10:00:00Z")

        violating = self.client("retrieve_scope_violation")
        with self.assertRaises(MemoryAdapterError) as ctx:
            violating.retrieve(
                work_item_id=self.work_item_id, query="migration"
            )
        self.assertEqual(ctx.exception.code, "SCOPE_VIOLATION")

    def test_zero_match_retrieval_is_successful_empty_result(self) -> None:
        # Assertion 24
        client = self.client("retrieve_empty")
        result = client.retrieve(
            work_item_id=self.work_item_id, query="nothing matches"
        )
        self.assertEqual(result.entries, ())
        self.assertIsInstance(result.entries, tuple)


class TransportErrorTestCase(MemoryAdapterClientTestCase):
    def test_missing_executable_maps_to_adapter_unavailable(self) -> None:
        # Assertion 20
        missing = str(Path(self.tmpdir) / "does-not-exist")
        client = MemoryAdapterClient([missing])
        with self.assertRaises(MemoryAdapterError) as ctx:
            client.store(
                work_item_id=self.work_item_id,
                session_id="session-001",
                content="c",
                provenance=self.provenance(),
            )
        self.assertEqual(ctx.exception.code, "ADAPTER_UNAVAILABLE")

    def test_timeout_maps_to_adapter_timeout(self) -> None:
        # Assertion 21
        client = self.client("timeout", timeout_seconds=0.3)
        with self.assertRaises(MemoryAdapterError) as ctx:
            client.store(
                work_item_id=self.work_item_id,
                session_id="session-001",
                content="c",
                provenance=self.provenance(),
            )
        self.assertEqual(ctx.exception.code, "ADAPTER_TIMEOUT")

    def test_non_zero_exit_maps_to_adapter_crashed(self) -> None:
        # Assertion 22
        client = self.client("crash")
        with self.assertRaises(MemoryAdapterError) as ctx:
            client.retrieve(
                work_item_id=self.work_item_id, query="migration"
            )
        self.assertEqual(ctx.exception.code, "ADAPTER_CRASHED")

    def test_no_retry_after_uncertain_store_timeout(self) -> None:
        # Assertion 23
        # The timeout is raised by the transport instead of being slept
        # through: under load a child can take longer than any fixed budget
        # just to reach the point where it records the invocation, so a real
        # 0.3s wait was a race whose loser lost the record, not the invariant.
        client = self.client("timeout", timeout_seconds=0.3)
        record = self.record

        def timeout_without_waiting(command, **kwargs):
            with open(record, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "argv": list(command)[1:],
                            "raw": kwargs.get("input", ""),
                        }
                    )
                    + "\n"
                )
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with mock.patch(
            "src.session_ops.memory_adapter_client.subprocess.run",
            timeout_without_waiting,
        ):
            with self.assertRaises(MemoryAdapterError) as ctx:
                client.store(
                    work_item_id=self.work_item_id,
                    session_id="session-001",
                    content="c",
                    provenance=self.provenance(),
                )
        self.assertEqual(ctx.exception.code, "ADAPTER_TIMEOUT")
        self.assertEqual(len(self.invocations()), 1)


class ErrorMappingTestCase(MemoryAdapterClientTestCase):
    def test_recognized_ma_error_codes_are_preserved(self) -> None:
        expected = {
            "INVALID_REQUEST",
            "INVALID_OPERATION",
            "INVALID_ID",
            "INVALID_SCOPE",
            "SCOPE_VIOLATION",
            "INVALID_CONTENT",
            "INVALID_PROVENANCE",
            "STORAGE_ERROR",
            "SERIALIZATION_ERROR",
            "INTERNAL_ERROR",
            "ADAPTER_UNAVAILABLE",
            "ADAPTER_TIMEOUT",
            "ADAPTER_CRASHED",
            "UNSUPPORTED_VERSION",
        }
        self.assertTrue(expected.issubset(KNOWN_ERROR_CODES))
        for code in sorted(expected - {"UNSUPPORTED_VERSION"}):
            with self.subTest(code=code):
                client = self.client("error_" + code)
                with self.assertRaises(MemoryAdapterError) as ctx:
                    client.store(
                        work_item_id=self.work_item_id,
                        session_id="session-001",
                        content="c",
                        provenance=self.provenance(),
                    )
                self.assertEqual(ctx.exception.code, code)
                self.assertIsNotNone(ctx.exception.request_id)

    def test_unknown_error_code_is_not_trusted(self) -> None:
        client = self.client("unknown_error_code")
        with self.assertRaises(MemoryAdapterError) as ctx:
            client.store(
                work_item_id=self.work_item_id,
                session_id="session-001",
                content="c",
                provenance=self.provenance(),
            )
        self.assertEqual(ctx.exception.code, "INTERNAL_ERROR")

    def test_store_failure_is_never_reported_as_success(self) -> None:
        # Assertion 26
        for scenario in ("error_STORAGE_ERROR", "error_INVALID_PROVENANCE"):
            with self.subTest(scenario=scenario):
                client = self.client(scenario)
                with self.assertRaises(MemoryAdapterError):
                    client.store(
                        work_item_id=self.work_item_id,
                        session_id="session-001",
                        content="c",
                        provenance=self.provenance(),
                    )
        # And a retrieve failure never becomes an empty success result.
        failing = self.client("error_STORAGE_ERROR")
        with self.assertRaises(MemoryAdapterError):
            failing.retrieve(
                work_item_id=self.work_item_id, query="migration"
            )


class DegradationTestCase(MemoryAdapterClientTestCase):
    def test_ma_failure_does_not_break_existing_so_functionality(self) -> None:
        # Assertion 25
        from src.session_ops.work_items import WorkItemStore

        db_dir = Path(self.tmpdir) / "so-db"
        db_dir.mkdir()
        store = WorkItemStore(db_dir / "work-items.sqlite3")
        store.initialize()
        item = store.create_work_item()
        store.link_session("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70", item.work_item_id)

        client = MemoryAdapterClient([str(Path(self.tmpdir) / "absent")])
        with self.assertRaises(MemoryAdapterError):
            client.retrieve(work_item_id=item.work_item_id, query="q")
        with self.assertRaises(MemoryAdapterError):
            client.store(
                work_item_id=item.work_item_id,
                session_id="session-001",
                content="c",
                provenance=self.provenance(),
            )

        # WorkItem operations continue unaffected.
        paused = store.set_status(item.work_item_id, "paused")
        self.assertEqual(paused.status, "paused")
        active_link = store.get_active_link("6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70")
        self.assertIsNotNone(active_link)
        assert active_link is not None
        self.assertEqual(active_link.work_item_id, item.work_item_id)
        self.assertEqual(
            [link.session_id for link in store.list_sessions(item.work_item_id)],
            ["6f1c2a3e-0d4b-4c8a-9f11-2b3c4d5e6f70"],
        )


if __name__ == "__main__":
    unittest.main()
