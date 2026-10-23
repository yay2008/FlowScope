"""data/ 快照备份: 打包内容、保留规则、到期判定与失败隔离。"""
import os
import tempfile
import threading
import unittest
import zipfile
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import backup

NOW = datetime(2026, 9, 28, 12, 0)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data = root / "data"
        self.dest = root / "backups"
        (self.data / "sub").mkdir(parents=True)
        (self.data / "a_v3.csv").write_bytes(b"time,buy\n1,2\n")
        (self.data / "sub" / "favorites.json").write_bytes(b"{}")
        (self.data / "a_v3.csv.tmp").write_bytes(b"half")

    def test_snapshot_contains_data_files_but_not_temporaries(self):
        path = backup.create_snapshot(self.data, self.dest, datetime(2026, 9, 28, 15, 30))
        self.assertEqual(path.name, "flowscope-data-20260928-153000.zip")
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(sorted(archive.namelist()), ["a_v3.csv", "sub/favorites.json"])
            self.assertEqual(archive.read("a_v3.csv"), b"time,buy\n1,2\n")
        self.assertEqual([item.name for item in self.dest.iterdir()], [path.name])

    def test_backup_dir_inside_data_is_not_packed_again(self):
        inner = self.data / "backups"
        backup.create_snapshot(self.data, inner, NOW)
        second = backup.create_snapshot(self.data, inner, NOW + timedelta(hours=1))
        with zipfile.ZipFile(second) as archive:
            self.assertFalse(any(name.startswith("backups/") for name in archive.namelist()))

    def test_live_alert_database_is_backed_up_as_consistent_sqlite(self):
        path = self.data / "alerts.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE events(id TEXT)")
            connection.execute("INSERT INTO events VALUES ('saved-alert')")
            connection.commit()
            snapshot = backup.create_snapshot(self.data, self.dest, NOW)
            with zipfile.ZipFile(snapshot) as archive:
                self.assertFalse(any(n.endswith(("-wal", "-shm")) for n in archive.namelist()))
                restored = self.dest / "restored.sqlite3"
                restored.write_bytes(archive.read("alerts.sqlite3"))
            db = sqlite3.connect(restored)
            try:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("SELECT id FROM events").fetchone()[0], "saved-alert")
            finally:
                db.close()
        finally:
            connection.close()

    def test_corrupt_database_is_copied_raw_and_does_not_abort_the_snapshot(self):
        (self.data / "alerts.sqlite3").write_bytes(b"not a database at all" * 10)
        (self.data / "favorites.json").write_text("[]", encoding="utf-8")
        with patch("builtins.print"):
            snapshot = backup.create_snapshot(self.data, self.dest, NOW)
        with zipfile.ZipFile(snapshot) as archive:
            self.assertEqual(archive.read("alerts.sqlite3"), b"not a database at all" * 10)
            self.assertEqual(archive.read("favorites.json"), b"[]")

    def test_database_locked_by_another_writer_is_copied_raw_instead_of_waiting_forever(self):
        path = self.data / "alerts.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE events(id TEXT)")
            connection.commit()
            connection.execute("BEGIN EXCLUSIVE")       # 别的进程长时间占着写锁
            done = {}

            def run():
                with patch("builtins.print") as printed:
                    done["snapshot"] = backup.create_snapshot(self.data, self.dest, NOW)
                done["log"] = printed.call_args.args[0]

            with patch.object(backup, "SQLITE_LOCK_WAIT_SEC", 0.2):
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                thread.join(10)
            self.assertFalse(thread.is_alive(), "锁不放时备份不能一直卡住")
            self.assertIn("直接复制", done["log"])
            with zipfile.ZipFile(done["snapshot"]) as archive:
                self.assertEqual(archive.read("alerts.sqlite3"), path.read_bytes())
                self.assertEqual(archive.read("sub/favorites.json"), b"{}")
        finally:
            connection.rollback()
            connection.close()

    def test_retention_keeps_last_week_and_newest_of_each_older_week(self):
        for days in [0, 1, 6, 8, 9, 15, 16, 100]:
            backup.create_snapshot(self.data, self.dest, NOW - timedelta(days=days))
        backup.prune(self.dest, NOW)
        kept = sorted((NOW - stamp).days for stamp, _ in backup.snapshots(self.dest))
        # 8/9 天前同在 9/14 那一周, 15/16 天前同在 9/7 那一周, 各留较新的一份
        self.assertEqual(kept, [0, 1, 6, 8, 15, 100])
        with patch.object(backup, "WEEKLY_KEEP", 2):
            backup.prune(self.dest, NOW)
        self.assertEqual(sorted((NOW - stamp).days for stamp, _ in backup.snapshots(self.dest)),
                         [0, 1, 6, 8, 15])

    def test_unrelated_files_in_backup_dir_are_left_alone(self):
        self.dest.mkdir()
        (self.dest / "notes.txt").write_bytes(b"keep me")
        backup.create_snapshot(self.data, self.dest, NOW - timedelta(days=400))
        with patch.object(backup, "WEEKLY_KEEP", 0):
            backup.prune(self.dest, NOW)
        self.assertEqual([item.name for item in self.dest.iterdir()], ["notes.txt"])

    def test_scheduler_backs_up_at_most_once_per_interval(self):
        scheduler = backup.BackupScheduler(lambda: self.data, lambda: self.dest, interval_sec=3600)
        self.assertIsNotNone(scheduler.run_once(NOW))
        self.assertIsNone(scheduler.run_once(NOW + timedelta(minutes=59)))
        self.assertIsNotNone(scheduler.run_once(NOW + timedelta(minutes=61)))
        status = scheduler.status()
        self.assertEqual(status["count"], 2)
        self.assertEqual(status["latest"], "flowscope-data-20260928-130100.zip")
        self.assertIsNone(status["error"])

    def test_failure_is_reported_instead_of_raised(self):
        blocker = Path(self.temp.name) / "not-a-dir"
        blocker.write_bytes(b"x")
        scheduler = backup.BackupScheduler(lambda: self.data, lambda: blocker / "backups")
        self.assertIsNone(scheduler.run_once(NOW))
        self.assertIsNotNone(scheduler.status()["error"])

    def test_non_os_error_is_reported_and_does_not_end_the_thread(self):
        """zipfile 也会抛 ValueError/BadZipFile; 不能让后台线程悄悄退出、状态还显示正常。"""
        scheduler = backup.BackupScheduler(lambda: self.data, lambda: self.dest)
        with patch.object(backup, "create_snapshot", side_effect=ValueError("timestamps before 1980")):
            self.assertIsNone(scheduler.run_once(NOW))
        self.assertIn("ValueError", scheduler.status()["error"])
        self.assertIsNotNone(scheduler.run_once(NOW))
        self.assertIsNone(scheduler.status()["error"])

    def test_snapshot_name_with_impossible_date_is_left_alone(self):
        self.dest.mkdir()
        stray = self.dest / "flowscope-data-20261399-250000.zip"
        stray.write_bytes(b"not ours")
        scheduler = backup.BackupScheduler(lambda: self.data, lambda: self.dest)
        self.assertIsNotNone(scheduler.run_once(NOW))
        with patch.object(backup, "WEEKLY_KEEP", 0):
            backup.prune(self.dest, NOW + timedelta(days=1000))
        self.assertEqual(scheduler.status()["count"], 0)
        self.assertTrue(stray.exists())

    def test_files_older_than_1980_are_still_backed_up(self):
        """zip 格式存不了 1980 年以前的时间; 某个文件的 mtime 被工具清零时不能让备份从此每次失败。"""
        os.utime(self.data / "a_v3.csv", (0, 0))
        path = backup.create_snapshot(self.data, self.dest, NOW)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read("a_v3.csv"), b"time,buy\n1,2\n")


if __name__ == "__main__":
    unittest.main()
