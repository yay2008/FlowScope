"""data/ 快照备份: 打包内容、保留规则、到期判定与失败隔离。"""
import tempfile
import unittest
import zipfile
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


if __name__ == "__main__":
    unittest.main()
