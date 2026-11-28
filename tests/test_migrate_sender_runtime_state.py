import os
import tempfile
import unittest
from pathlib import Path

from tools.migrate_sender_runtime_state import STATE_FILES, migrate_state


class MigrateSenderRuntimeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "old-cli"
        self.target = self.root / "runtime"
        self.source.mkdir()
        self.target.mkdir()
        for index, name in enumerate(STATE_FILES):
            (self.source / name).write_text("state-%d" % index, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_copies_only_declared_state_files_and_is_idempotent(self):
        first = migrate_state(self.source, self.target)
        self.assertEqual([row[1] for row in first], ["COPIED"] * len(STATE_FILES))
        for name in STATE_FILES:
            self.assertEqual(
                (self.target / name).read_text(encoding="utf-8"),
                (self.source / name).read_text(encoding="utf-8"),
            )
            self.assertEqual((self.target / name).stat().st_mode & 0o777, 0o660)
        second = migrate_state(self.source, self.target)
        self.assertEqual([row[1] for row in second], ["UNCHANGED"] * len(STATE_FILES))

    def test_refuses_to_overwrite_different_destination(self):
        (self.target / STATE_FILES[0]).write_text("newer", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "refusing overwrite"):
            migrate_state(self.source, self.target)

    def test_refuses_symlink_source(self):
        source = self.source / STATE_FILES[0]
        replacement = self.source / "real-state"
        source.rename(replacement)
        source.symlink_to(replacement)
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            migrate_state(self.source, self.target)

    def test_refuses_nested_source_and_target(self):
        nested = self.source / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(RuntimeError, "separate"):
            migrate_state(self.source, nested)


if __name__ == "__main__":
    unittest.main()
