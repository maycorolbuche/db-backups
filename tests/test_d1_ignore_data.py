import io
import json
import os
import sqlite3
import time
import unittest
from unittest.mock import patch

from db_backups import BackupError, export_d1, filter_d1_data, validate_table_filters


class D1IgnoreDataTests(unittest.TestCase):
    sql = """
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE sessions(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT);
CREATE TABLE charge_emails(id INTEGER PRIMARY KEY, value TEXT);
CREATE TABLE charge_email_attempts(id INTEGER PRIMARY KEY, value TEXT);
CREATE TABLE users(id INTEGER PRIMARY KEY, value TEXT);
CREATE INDEX session_value ON sessions(value);
INSERT INTO sessions VALUES(1,'private; session');
INSERT INTO "charge_emails" VALUES(1,'email');
INSERT OR REPLACE INTO [charge_email_attempts] VALUES(1,'attempt');
INSERT INTO users VALUES(1,'Olá; mundo
INSERT INTO sessions VALUES(999,''texto'');
');
CREATE VIEW user_names AS SELECT value FROM users;
CREATE TRIGGER add_session AFTER INSERT ON users BEGIN
  INSERT INTO sessions(value) VALUES('new; session');
END;
COMMIT;
-- trailer
"""
    ignored = ["sessions", "charge_emails", "charge_email_attempts"]

    def filter(self, sql):
        output = io.BytesIO()
        filter_d1_data(io.BytesIO(sql.encode()), output, self.ignored, time.monotonic() + 10)
        return output.getvalue()

    def test_restores_empty_tables_with_schema_and_other_data(self):
        filtered = self.filter(self.sql)
        with sqlite3.connect(":memory:") as db:
            db.executescript(filtered.decode())
            for table in self.ignored:
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertIn("Olá; mundo", db.execute("SELECT value FROM users").fetchone()[0])
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_schema WHERE name='session_value'").fetchone())
            self.assertIsNotNone(db.execute("SELECT * FROM user_names").fetchone())
            db.execute("INSERT INTO users VALUES(2,'test')")
            self.assertEqual(db.execute("SELECT value FROM sessions").fetchone()[0], "new; session")

    def test_stream_handles_utf8_and_sql_across_small_chunks(self):
        class SmallChunks(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 7))
        output = io.BytesIO()
        filter_d1_data(SmallChunks(self.sql.encode()), output, self.ignored, time.monotonic() + 10)
        self.assertEqual(output.getvalue(), self.filter(self.sql))

    def test_missing_table_or_truncated_sql_is_an_error(self):
        with self.assertRaisesRegex(BackupError, "não encontradas"):
            filter_d1_data(io.BytesIO(b"SELECT 1;"), io.BytesIO(), ["missing"], time.monotonic() + 10)
        with self.assertRaisesRegex(BackupError, "incompleta"):
            self.filter(self.sql + "INSERT INTO users VALUES(")

    def test_validates_ignore_data_on_d1(self):
        validate_table_filters("mrpag", {"type": "d1", "ignore_data": self.ignored})
        with self.assertRaises(BackupError):
            validate_table_filters("mrpag", {"type": "d1", "ignore_table": ["sessions"]})

    def test_download_filters_before_sending_sql_to_hash_pipeline(self):
        config = {"credentials_env": "D1_TOKEN", "database_id": "fixture", "ignore_data": self.ignored}
        complete = io.BytesIO(json.dumps({
            "success": True, "result": {"signed_url": "https://example.com/dump.sql"},
        }).encode())
        download = io.BytesIO(self.sql.encode())
        download.headers = {"Content-Length": str(len(self.sql.encode()))}
        output = io.BytesIO()
        with patch.dict(os.environ, {"D1_TOKEN": "token", "R2_ACCOUNT_ID": "a" * 32}), patch(
            "db_backups.urlopen", side_effect=[complete, download]
        ):
            export_d1(config, output, time.monotonic() + 10)
        self.assertEqual(output.getvalue(), self.filter(self.sql))
