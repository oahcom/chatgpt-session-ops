import hashlib
import json
import sqlite3
import unittest
from pathlib import Path

from scripts.archive_legacy_sender_state import archive


class ArchiveLegacySenderStateTests(unittest.TestCase):
    def test_archives_full_database_without_mutating_source(self):
        with __import__("tempfile").TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sender.sqlite3"
            destination = root / "archive" / "sender.snapshot.sqlite3"
            with sqlite3.connect(source) as conn:
                conn.executescript("""
                    CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY, operation TEXT, status TEXT,
                        created_at TEXT, started_at TEXT, attempt_count INTEGER,
                        outcome_unknown INTEGER, prompt TEXT
                    );
                    CREATE TABLE job_event_outbox (
                        event_id TEXT PRIMARY KEY, status TEXT, payload_json TEXT
                    );
                    INSERT INTO jobs VALUES
                        ('job-running','session.send','running','t1','t2',201,0,'private prompt'),
                        ('job-queued','session.create','queued','t3',NULL,7,0,'another private prompt');
                    INSERT INTO job_event_outbox VALUES ('event-1','pending','{"private":"payload"}');
                """)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

            report = archive(source, destination)

            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
            self.assertEqual(report["integrity_check"], "ok")
            self.assertEqual(report["job_statuses"], {"queued": 1, "running": 1})
            self.assertEqual(report["outbox_statuses"], {"pending": 1})
            self.assertEqual(report["table_counts"]["jobs"], 2)
            self.assertEqual(len(report["nonterminal_jobs"]), 2)
            self.assertNotIn("private prompt", json.dumps(report))
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            manifest = destination.with_name(destination.name + ".manifest.json")
            self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(manifest.read_text())["sha256"], report["sha256"])
            with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT count(*) FROM job_event_outbox").fetchone()[0], 1)

    def test_refuses_to_overwrite_existing_archive(self):
        with __import__("tempfile").TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            destination = root / "archive.sqlite3"
            sqlite3.connect(source).close()
            destination.write_bytes(b"preserve")
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                archive(source, destination)
            self.assertEqual(destination.read_bytes(), b"preserve")

    def test_rejects_symlink_source(self):
        with __import__("tempfile").TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            alias = root / "alias.sqlite3"
            sqlite3.connect(source).close()
            alias.symlink_to(source)
            with self.assertRaisesRegex(RuntimeError, "not a symlink"):
                archive(alias, root / "archive.sqlite3")

    def test_rejects_world_accessible_archive_directory(self):
        with __import__("tempfile").TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            destination_dir = root / "shared"
            destination_dir.mkdir(mode=0o755)
            destination_dir.chmod(0o755)
            sqlite3.connect(source).close()
            with self.assertRaisesRegex(RuntimeError, "group or others"):
                archive(source, destination_dir / "archive.sqlite3")


if __name__ == "__main__":
    unittest.main()
