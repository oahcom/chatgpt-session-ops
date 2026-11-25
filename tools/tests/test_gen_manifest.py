from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import gen_manifest


class GenerateManifestDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

        self.repo = self.base / "repo"
        self.repo.mkdir()

        subprocess.run(
            ["git", "init", str(self.repo)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(self.repo),
                "config", "user.email", "test@example.invalid",
            ],
            check=True,
        )

        (self.repo / "app.py").write_text(
            "print('test')\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "app.py"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "test"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            text=True,
        ).strip()

        self.paths_file = self.base / "paths.txt"
        self.paths_file.write_text("app.py\n", encoding="utf-8")

        self.db_path = self.base / "work-items.sqlite3"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE session_links (session_id TEXT)"
            )
            conn.execute(
                "CREATE TABLE authority_landing_events (attempt_id TEXT)"
            )
            conn.commit()
        finally:
            conn.close()

        self.manifest_path = self.base / "manifest.json"

    def _base_args(self) -> list[str]:
        return [
            "--repo", str(self.repo),
            "--commit", self.commit,
            "--paths-from", str(self.paths_file),
            "--out", str(self.manifest_path),
        ]

    def test_database_contract_requires_path_and_writes_database_shape(
        self,
    ) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = gen_manifest.main(
                self._base_args()
                + [
                    "--db-required-column",
                    "session_links.session_id",
                ]
            )

        self.assertEqual(result, 1)
        self.assertIn("--db-path", stderr.getvalue())
        self.assertFalse(self.manifest_path.exists())

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = gen_manifest.main(
                self._base_args()
                + [
                    "--db-path", str(self.db_path),
                    "--db-required-column",
                    "session_links.session_id",
                    "--db-required-column",
                    "authority_landing_events.attempt_id",
                ]
            )

        self.assertEqual(result, 0, stderr.getvalue())
        manifest = json.loads(
            self.manifest_path.read_text(encoding="utf-8")
        )

        self.assertIn("database", manifest)
        self.assertNotIn("database_schema", manifest)
        self.assertEqual(
            manifest["database"]["path"],
            str(self.db_path),
        )
        self.assertEqual(
            manifest["database"]["required_columns"],
            {
                "session_links": ["session_id"],
                "authority_landing_events": ["attempt_id"],
            },
        )
        self.assertEqual(
            manifest["database"]["schema_version"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
