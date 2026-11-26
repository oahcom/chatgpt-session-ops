"""Guard tests for the trusted landing path and the declared trusted root.

Issue #135. Every guard pinned here was mutation-proven: replace the guard
statement with ``pass`` and at least one assertion in this file fails. The
two areas are the trusted-send orchestration in ``chatgpt_send.py`` and the
``SO_MCP_TRUSTED_ROOT`` preflight that must pass before any
``src.session_ops`` import.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts import chatgpt_send as sender
from src.session_ops.authority import (
    Principal,
    PrincipalRecord,
    SecureWorkItemStore,
    StaticPrincipalDirectory,
)
from tests.test_send_prompt_for_work_item import FakeAPI, cid

TRUSTED_ORIGIN = "https://github.com/oahcom/chatgpt-session-ops"
REQUIRED_SOURCES = ("authority.py", "work_items.py")


def git(*args):
    return subprocess.run(
        ["git", *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


class LandingGuardTests(unittest.TestCase):
    """Guards inside _send_prompt_impl and send_prompt_for_work_item."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        db = self.root / "authority.sqlite3"
        self.store = SecureWorkItemStore(
            db,
            principal_directory=StaticPrincipalDirectory(
                [PrincipalRecord("alice", "tenant-1")]
            ),
        )
        self.store.initialize()
        self.principal = Principal("tenant-1", "alice")
        self.item = self.store.create_work_item(self.principal)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("guard prompt", encoding="utf-8")

    def event_count(self):
        conn = self.store._connect()
        try:
            return conn.execute(
                "SELECT count(*) FROM authority_landing_events"
            ).fetchone()[0]
        finally:
            conn.close()

    def test_landing_requires_attempt_identity(self):
        """Missing or malformed attempt_id must not create a landing event."""
        bad_values = [None, 123, "", "not-an-attempt"]
        for bad in bad_values:
            with self.subTest(attempt_id=bad):
                api = FakeAPI([cid()], root=self.root)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    result = sender._send_prompt_impl(
                        api,
                        None,
                        str(self.prompt),
                        new_chat=True,
                        thinking=False,
                        text="guard prompt",
                        _landing_store=self.store,
                        _landing_work_item_id=self.item.work_item_id,
                        _landing_attempt_id=bad,
                    )
                self.assertEqual(result[0], 200)
                self.assertEqual(self.event_count(), 0)
                self.assertIn(
                    "trusted landing attempt_id is missing",
                    out.getvalue(),
                )

    def test_store_must_be_secure_work_item_store(self):
        api = FakeAPI([cid()], root=self.root)
        with self.assertRaises(TypeError) as ctx:
            sender.send_prompt_for_work_item(
                api,
                object(),
                self.principal,
                self.item.work_item_id,
                None,
                str(self.prompt),
            )
        self.assertIn("store must be SecureWorkItemStore", str(ctx.exception))
        self.assertEqual(self.event_count(), 0)

    def test_principal_must_be_verified(self):
        api = FakeAPI([cid()], root=self.root)
        with self.assertRaises(TypeError) as ctx:
            sender.send_prompt_for_work_item(
                api,
                self.store,
                "alice",
                self.item.work_item_id,
                None,
                str(self.prompt),
            )
        self.assertIn(
            "principal must be a verified Principal", str(ctx.exception)
        )
        self.assertEqual(self.event_count(), 0)

    def test_anonymous_send_cannot_establish_trusted_landing(self):
        api = FakeAPI([cid()], root=self.root)
        api.is_anon = True
        # The prompt does not exist: reaching any later step would raise a
        # different error, so the guard is the only way to see this message.
        missing = str(self.root / "no-such-prompt.txt")
        with self.assertRaises(ValueError) as ctx:
            sender.send_prompt_for_work_item(
                api,
                self.store,
                self.principal,
                self.item.work_item_id,
                None,
                missing,
            )
        self.assertIn(
            "anonymous sends cannot establish trusted landing evidence",
            str(ctx.exception),
        )
        self.assertEqual(self.event_count(), 0)

    def test_send_exception_propagates_after_end_log_backfill(self):
        api = FakeAPI([cid()], root=self.root)
        begin = self.root / "begin.json"
        with patch.object(sender, "load_prompt_text", return_value="text"):
            with patch.object(
                sender, "log_begin", return_value=("turn-1", str(begin))
            ):
                with patch.object(sender, "log_end") as log_end:
                    with patch.object(
                        sender,
                        "_send_prompt_impl",
                        side_effect=RuntimeError("transport exploded"),
                    ):
                        with self.assertRaises(RuntimeError) as ctx:
                            sender.send_prompt_for_work_item(
                                api,
                                self.store,
                                self.principal,
                                self.item.work_item_id,
                                None,
                                str(self.prompt),
                            )
        self.assertEqual(str(ctx.exception), "transport exploded")
        log_end.assert_called_once()
        self.assertIn("transport exploded", log_end.call_args.kwargs["error"])
        self.assertEqual(self.event_count(), 0)


class TrustedRootGuardTests(unittest.TestCase):
    """Every rejection branch of require_trusted_worktree_root."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Resolve up front: Windows may hand back an 8.3 short path that the
        # guard normalizes, and the messages quote the normalized path.
        self.root = Path(tmp.name).resolve()

    @contextlib.contextmanager
    def declared(self, value):
        """Set SO_MCP_TRUSTED_ROOT for one call; None means unset."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SO_MCP_TRUSTED_ROOT", None)
            if value is not None:
                os.environ["SO_MCP_TRUSTED_ROOT"] = str(value)
            yield

    def make_repo(self, name="declared", origin=TRUSTED_ORIGIN):
        root = self.root / name
        result = git("init", "-q", str(root))
        self.assertEqual(result.returncode, 0, result.stderr)
        if origin is not None:
            result = git("-C", str(root), "remote", "add", "origin", origin)
            self.assertEqual(result.returncode, 0, result.stderr)
        sources = root / "src" / "session_ops"
        sources.mkdir(parents=True)
        for source in REQUIRED_SOURCES:
            (sources / source).write_text("# fixture\n", encoding="utf-8")
        return root

    def rejection(self, value):
        with self.declared(value):
            with self.assertRaises(RuntimeError) as ctx:
                sender.require_trusted_worktree_root()
        return str(ctx.exception)

    def test_root_must_be_declared(self):
        message = self.rejection(None)
        self.assertIn("SO_MCP_TRUSTED_ROOT is not set", message)

    def test_root_must_be_absolute(self):
        message = self.rejection("relative/root")
        self.assertIn("SO_MCP_TRUSTED_ROOT must be an absolute path", message)

    def test_root_must_resolve_to_an_existing_directory(self):
        message = self.rejection(self.root / "missing")
        self.assertIn(
            "SO_MCP_TRUSTED_ROOT does not resolve to an existing directory",
            message,
        )

    def test_root_must_be_a_directory(self):
        plain_file = self.root / "not-a-directory"
        plain_file.write_text("x", encoding="utf-8")
        message = self.rejection(plain_file)
        self.assertIn("SO_MCP_TRUSTED_ROOT is not a directory", message)
        self.assertIn(str(plain_file), message)

    def test_root_must_carry_git_worktree_marker(self):
        empty = self.root / "declared"
        empty.mkdir()
        message = self.rejection(empty)
        self.assertIn("declared root has no .git worktree marker", message)

    def test_root_must_contain_required_sources(self):
        bare = self.root / "declared"
        (bare / ".git").mkdir(parents=True)
        message = self.rejection(bare)
        self.assertIn(
            "declared worktree is missing required source file", message
        )
        self.assertIn("authority.py", message)

    def test_git_failure_in_declared_root_is_rejected(self):
        # A .git directory that is not a repository: the marker exists and the
        # required sources exist, so the git probe itself must fail closed.
        broken = self.root / "declared"
        (broken / ".git").mkdir(parents=True)
        sources = broken / "src" / "session_ops"
        sources.mkdir(parents=True)
        for source in REQUIRED_SOURCES:
            (sources / source).write_text("# fixture\n", encoding="utf-8")
        message = self.rejection(broken)
        self.assertIn(
            "git rev-parse --show-toplevel failed in declared root", message
        )

    def test_root_must_be_the_worktree_top_level(self):
        root = self.make_repo()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        result = git(
            "-C", str(root), "config", "core.worktree", str(elsewhere)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # git resolves the declared directory but reports another top level.
        message = self.rejection(root)
        self.assertIn(
            "declared root is not the Git worktree top level", message
        )
        self.assertIn(str(elsewhere), message)

    def test_origin_must_identify_the_trusted_repository(self):
        root = self.make_repo(
            origin="https://github.com/attacker/chatgpt-session-ops.git"
        )
        message = self.rejection(root)
        self.assertIn(
            "Git origin does not identify the trusted repository", message
        )
        self.assertIn("expected=%s" % TRUSTED_ORIGIN, message)

    def test_trusted_root_is_accepted(self):
        origins = [
            TRUSTED_ORIGIN,
            TRUSTED_ORIGIN + ".git",
            "git@github.com:oahcom/chatgpt-session-ops.git",
            "ssh://git@github.com/oahcom/chatgpt-session-ops",
        ]
        original_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(
            slice(None), original_path
        ))
        for origin in origins:
            with self.subTest(origin=origin):
                root = self.make_repo(
                    name="wf-%d" % (uuid.uuid4().int % 100000), origin=origin
                )
                with self.declared(root):
                    accepted = sender.require_trusted_worktree_root()
                self.assertEqual(accepted, root.resolve())


if __name__ == "__main__":
    unittest.main()
