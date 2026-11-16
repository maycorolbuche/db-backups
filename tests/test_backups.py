import gzip
import hashlib
import io
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from db_backups import BackupError, OBJECT_NAME, due, make_archive, retention_plan, run_backup, send_telegram, stamp


def backup(key, size):
    return {"Key": key, "Size": size}


class RetentionTests(unittest.TestCase):
    def test_third_copy_hits_quota(self):
        older = [backup("recent", 200), backup("old", 200)]
        retained, expired, limited = retention_plan(older, backup("new", 200), 10, 500)
        self.assertEqual([obj["Key"] for obj in retained], ["new", "recent"])
        self.assertEqual(sum(obj["Size"] for obj in retained), 400)
        self.assertEqual(expired, [older[1]])
        self.assertTrue(limited)

    def test_file_larger_than_quota(self):
        with self.assertRaisesRegex(BackupError, "Espaço insuficiente"):
            retention_plan([], backup("new", 300), 10, 100)

    def test_count_without_quota(self):
        retained, expired, limited = retention_plan([backup("old", 200)], backup("new", 200), 1, None)
        self.assertEqual(retained, [backup("new", 200)])
        self.assertEqual(expired, [backup("old", 200)])
        self.assertFalse(limited)

    def test_counts_are_per_bank_and_use_verified_copies(self):
        store = Mock()
        store.backups.return_value = [
            backup("backups/test/20261116T101000000000Z_manual_a.sql.gz.age", 400_000),
            backup("backups/test/20261116T100000000000Z_manual_b.sql.gz.age", 400_000),
        ]
        db = {"keep": 10, "max_storage_mb": 1}
        with patch("db_backups.make_archive", side_effect=lambda db, directory, output: output.write_bytes(b"x" * 400_000)):
            self.assertTrue(run_backup(store, "test", db, timezone.utc, "manual"))
        self.assertEqual(store.method_calls[1][0], "upload")
        self.assertEqual(store.method_calls[2][0], "mark_verified")
        store.remove_backup.assert_called_once_with(store.backups.return_value[-1]["Key"])
        self.assertIn("2 de 10", store.notify.call_args.args[1])

    def test_oversize_does_not_upload_or_rotate(self):
        store = Mock()
        store.backups.return_value = []
        db = {"keep": 10, "max_storage_mb": 1}
        with patch("db_backups.make_archive", side_effect=lambda db, directory, output: output.write_bytes(b"x" * 1_000_001)):
            self.assertFalse(run_backup(store, "test", db, timezone.utc, "manual"))
        store.upload.assert_not_called()
        store.remove_backup.assert_not_called()
        self.assertIn("Espaço insuficiente", store.notify.call_args.args[1])

    def test_failed_upload_does_not_delete_previous_copy(self):
        store = Mock()
        store.backups.return_value = [backup("backups/test/20261116T100000000000Z_manual_b.sql.gz.age", 4)]
        store.upload.side_effect = BackupError("Upload falhou.")
        with patch("db_backups.make_archive", side_effect=lambda db, directory, output: output.write_bytes(b"data")):
            self.assertFalse(run_backup(store, "test", {"keep": 1}, timezone.utc, "manual"))
        store.mark_verified.assert_not_called()
        self.assertNotEqual(store.remove_backup.call_args.args[0], store.backups.return_value[0]["Key"])


class ScheduleTests(unittest.TestCase):
    def test_monthly_schedule_in_sao_paulo_includes_exact_minute(self):
        zone = ZoneInfo("America/Sao_Paulo")
        moment = datetime(2026, 11, 1, 8, 15, tzinfo=timezone.utc)
        self.assertEqual(due({"schedule": "15 5 1 * *"}, zone, moment), stamp(moment))


class TelegramTests(unittest.TestCase):
    def test_blank_omits_thread_and_id_routes_to_topic(self):
        for thread, expected in [("", None), ("  ", None), (" 123 ", 123)]:
            with self.subTest(thread=thread), patch.dict("os.environ", {
                "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "-100123",
                "TELEGRAM_MESSAGE_THREAD_ID": thread,
            }), patch("db_backups.urlopen", return_value=io.BytesIO(b'{"ok":true}')) as request:
                send_telegram("Backup concluído")
                data = json.loads(request.call_args.args[0].data)
                self.assertEqual(data["chat_id"], "-100123")
                if expected is None:
                    self.assertNotIn("message_thread_id", data)
                else:
                    self.assertEqual(data["message_thread_id"], expected)


class StorageTests(unittest.TestCase):
    def test_upload_verifies_bytes_returned_by_r2(self):
        import hashlib
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from botocore.response import StreamingBody

        from db_backups import Storage

        store = Storage.__new__(Storage)
        store.bucket = "backups"
        store.s3 = Mock()
        with TemporaryDirectory() as directory:
            archive = Path(directory) / "backup.age"
            archive.write_bytes(b"encrypted")
            digest = hashlib.sha256(b"encrypted").hexdigest()
            store.s3.get_object.return_value = {
                "Body": StreamingBody(io.BytesIO(b"encrypted"), 9), "ContentLength": 9,
            }
            store.upload(archive, "backup.age", digest)
            store.s3.get_object.return_value = {
                "Body": StreamingBody(io.BytesIO(b"corrupted"), 9), "ContentLength": 9,
            }
            with self.assertRaisesRegex(BackupError, "integridade"):
                store.upload(archive, "backup.age", digest)


class EncryptionOptionTests(unittest.TestCase):
    def test_both_archive_formats_are_recognized(self):
        basename = "20261116T101000000000Z_manual_" + "a" * 32
        for suffix in (".sql.gz", ".sql.gz.age"):
            with self.subTest(suffix=suffix):
                self.assertIsNotNone(OBJECT_NAME.fullmatch(basename + suffix))
        self.assertIsNone(OBJECT_NAME.fullmatch(basename + ".sql.gz.tmp"))

    def test_plain_backup_only_requires_gzip(self):
        sql = b"SELECT 1;\n"
        credentials = {"database": "app", "user": "test", "password": "test"}

        def dump(command, *, stdout, **kwargs):
            self.assertEqual(command[0], "mysqldump")
            stdout.write(sql)
            stdout.flush()
            return Mock(returncode=0)

        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "AGE_RECIPIENT": "",
            "TEST_DB": json.dumps(credentials),
        }), patch("db_backups.subprocess.run", side_effect=dump):
            output = Path(directory) / "backup.sql.gz"
            config = {"type": "mysql", "credentials_env": "TEST_DB", "encrypt": False}
            digest = make_archive(config, output.parent, output)
            self.assertEqual(gzip.decompress(output.read_bytes()), sql)
            self.assertEqual(digest, hashlib.sha256(sql).hexdigest())

    def test_backup_defaults_to_encryption(self):
        credentials = {"database": "app", "user": "test", "password": "test"}
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "AGE_RECIPIENT": "",
            "TEST_DB": json.dumps(credentials),
        }):
            output = Path(directory) / "backup.sql.gz.age"
            with self.assertRaisesRegex(BackupError, "AGE_RECIPIENT"):
                make_archive({"type": "mysql", "credentials_env": "TEST_DB"}, output.parent, output)


class DeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.slot = "20261116T101000000000Z"
        self.previous = "backups/test/20261116T100000000000Z_manual_" + "a" * 32 + ".sql.gz"
        self.store = Mock()
        self.store.backups.return_value = [backup(self.previous, 4)]
        self.store.read.return_value = {}
        self.config = {"keep": 2, "encrypt": False}
        self.digest = hashlib.sha256(b"SELECT 1;").hexdigest()

    def make_archive(self, db, directory, output):
        output.write_bytes(b"data")
        return self.digest

    def test_identical_scheduled_dump_records_completion_and_notifies(self):
        self.store.metadata.return_value = {"sql-sha256": self.digest}
        with patch("db_backups.make_archive", side_effect=self.make_archive):
            self.assertTrue(run_backup(self.store, "test", self.config, timezone.utc, self.slot))
        self.store.metadata.assert_called_once_with(self.previous)
        self.store.upload.assert_not_called()
        self.store.mark_verified.assert_not_called()
        self.store.remove_backup.assert_not_called()
        self.assertIn("sem alterações", self.store.notify.call_args.args[1])
        self.store.write.assert_any_call("scheduled/test.json", {"slot": self.slot})
        self.assertEqual(self.store.write.call_args.args[1]["outcome"], "unchanged")

    def test_changed_or_legacy_dump_is_saved(self):
        for metadata in ({"sql-sha256": "different"}, {}):
            with self.subTest(metadata=metadata):
                self.store.reset_mock()
                self.store.metadata.return_value = metadata
                with patch("db_backups.make_archive", side_effect=self.make_archive):
                    self.assertTrue(run_backup(self.store, "test", self.config, timezone.utc, self.slot))
                self.store.upload.assert_called_once()
                self.assertEqual(self.store.upload.call_args.args[3], self.digest)

    def test_manual_or_different_format_saves_a_copy(self):
        for slot, encrypted in (("manual", False), (self.slot, True)):
            with self.subTest(slot=slot, encrypted=encrypted):
                self.store.reset_mock()
                self.store.metadata.return_value = {"sql-sha256": self.digest}
                with patch("db_backups.make_archive", side_effect=self.make_archive):
                    config = dict(self.config, encrypt=encrypted)
                    self.assertTrue(run_backup(self.store, "test", config, timezone.utc, slot))
                self.store.metadata.assert_not_called()
                self.store.upload.assert_called_once()
