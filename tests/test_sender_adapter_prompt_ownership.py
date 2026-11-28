"""Sender adapter prompt directory ownership regressions (source snapshot)."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENDER_ROOT = ROOT / "deploy" / "source-snapshot" / "2026-11-28" / "so-sender"
sys.path.insert(0, str(SENDER_ROOT))

from sender_gateway.sender_adapter import (  # noqa: E402
    CODE_DEPENDENCY_UNAVAILABLE,
    ChatGPTSenderAdapter,
    SenderError,
)


class PromptDirectoryOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sender-adapter-181-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cli = self.base / "fake_cli.py"
        self.cli.write_text("pass\n", encoding="utf-8")

    def test_caller_owned_root_and_sibling_adapter_survive_close(self):
        shared = self.base / "prompts"
        shared.mkdir()
        marker = shared / "caller-owned.txt"
        marker.write_text("keep", encoding="utf-8")

        first = ChatGPTSenderAdapter(str(self.cli), prompt_dir=str(shared))
        second = ChatGPTSenderAdapter(str(self.cli), prompt_dir=str(shared))
        first_root = Path(first._prompt_root)
        second_root = Path(second._prompt_root)

        first.close()
        second_prompt = Path(second._write_prompt("still alive"))

        self.assertTrue(shared.is_dir())
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertFalse(first_root.exists())
        self.assertTrue(second_root.is_dir())
        self.assertEqual(second_prompt.read_text(encoding="utf-8"), "still alive")

        second.close()
        self.assertTrue(shared.is_dir())
        self.assertFalse(second_root.exists())

    def test_internally_created_root_is_cleaned_and_close_is_idempotent(self):
        adapter = ChatGPTSenderAdapter(str(self.cli))
        owned_root = Path(adapter._prompt_root)
        adapter._write_prompt("temporary")
        self.assertTrue(owned_root.is_dir())

        adapter.close()
        adapter.close()

        self.assertFalse(owned_root.exists())

    def test_missing_prompt_directory_returns_actionable_error(self):
        shared = self.base / "prompts"
        adapter = ChatGPTSenderAdapter(str(self.cli), prompt_dir=str(shared))
        shutil.rmtree(adapter._prompt_root)

        with self.assertRaises(SenderError) as raised:
            adapter._write_prompt("cannot write")

        self.assertEqual(raised.exception.code, CODE_DEPENDENCY_UNAVAILABLE)
        self.assertIn("prompt storage unavailable", raised.exception.detail)
        adapter.close()

    def test_new_adapter_can_write_after_previous_instance_closes(self):
        shared = self.base / "prompts"
        first = ChatGPTSenderAdapter(str(self.cli), prompt_dir=str(shared))
        first.close()

        second = ChatGPTSenderAdapter(str(self.cli), prompt_dir=str(shared))
        prompt = Path(second._write_prompt("after restart"))

        self.assertTrue(prompt.is_file())
        self.assertEqual(prompt.read_text(encoding="utf-8"), "after restart")
        second.close()
        self.assertTrue(shared.is_dir())


if __name__ == "__main__":
    unittest.main()
