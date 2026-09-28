# -*- coding: utf-8 -*-
"""data/ 的定期快照备份: 买卖量历史没法从 TqSdk 重新生成(不提供历史 tick), 丢了就是丢了。

- 每个快照是一个 zip(flowscope-data-YYYYmmdd-HHMMSS.zip), 先写临时文件再改名, 不会留下半个包;
  采集线程只追加写, 快照里最后一行可能是半行, 读历史时本来就会跳过。
- 服务运行时每 BACKUP_INTERVAL_SEC 最多做一次, 启动时到期就补做。dev 模式每次重载都是
  进程重启, 所以不能"每次启动都备份", 否则保留份数很快被重载刷掉。
- 保留最近 RECENT_DAYS 天的全部快照; 更早的每个 ISO 周只留最新一份, 最多 WEEKLY_KEEP 周。
  历史只追加, 旧快照是新快照的子集, 留着它们只为了"某次写坏了数据, 过几天才发现"。
- 备份目录默认是项目下的 backups/, 可用环境变量 FLOWSCOPE_BACKUP_DIR(可写在 .env)指到
  别的盘或同步盘; 与 data/ 同盘只防误删和写坏, 不防坏盘。

手动或计划任务运行: .\\.venv\\Scripts\\python.exe backup.py [--dest 目录]
恢复: 停掉服务, 把某个快照解压覆盖到 data/ 再启动。
"""
from __future__ import annotations

import argparse
import os
import re
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_INTERVAL_SEC = 6 * 3600
BACKUP_CHECK_SEC = 600
RECENT_DAYS = 7
WEEKLY_KEEP = 12
_PREFIX = "flowscope-data-"
_NAME = re.compile(re.escape(_PREFIX) + r"(\d{8}-\d{6})\.zip\Z")
_STAMP = "%Y%m%d-%H%M%S"


def default_dest() -> str:
    return os.getenv("FLOWSCOPE_BACKUP_DIR") or os.path.join(BASE_DIR, "backups")


def snapshots(dest) -> list[tuple[datetime, Path]]:
    """已有快照, 新的在前; 不认识的文件一律不碰。"""
    found = []
    folder = Path(dest)
    if not folder.is_dir():
        return found
    for path in folder.iterdir():
        match = _NAME.match(path.name)
        if not match or not path.is_file():
            continue
        try:
            stamp = datetime.strptime(match.group(1), _STAMP)
        except ValueError:
            continue    # 形如快照、日期却不存在(手工改过名), 不是我们写的
        found.append((stamp, path))
    return sorted(found, reverse=True)


def create_snapshot(source, dest, now: datetime | None = None) -> Path:
    """把 source 下的全部文件(不含 *.tmp 中间文件)打成一个 zip, 返回快照路径。

    备份目录被配置在 source 里面时跳过它, 否则每个快照都会把之前的快照再包一遍。
    """
    now = now or datetime.now()
    folder = Path(dest)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{_PREFIX}{now.strftime(_STAMP)}.zip"
    temporary = target.with_name(target.name + ".tmp")
    root = Path(source)
    own = folder.resolve()
    try:
        # zip 存不了 1980 年以前的修改时间; 某个文件的 mtime 被清零时按 1980 记, 不让备份从此每次失败。
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED,
                             strict_timestamps=False) as archive:
            for path in sorted(root.rglob("*")):
                if (path.is_file() and path.suffix != ".tmp"
                        and own not in path.resolve().parents):
                    archive.write(path, path.relative_to(root).as_posix())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def prune(dest, now: datetime | None = None) -> list[Path]:
    """按保留规则删除多余快照, 返回被删的路径。"""
    now = now or datetime.now()
    keep, weeks = set(), set()
    for stamp, path in snapshots(dest):
        if now - stamp <= timedelta(days=RECENT_DAYS):
            keep.add(path)
            continue
        week = stamp.isocalendar()[:2]
        if week not in weeks and len(weeks) < WEEKLY_KEEP:
            weeks.add(week)
            keep.add(path)
    removed = []
    for _, path in snapshots(dest):
        if path not in keep:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


class BackupScheduler:
    """服务内的后台备份线程; 目录用 provider 现取, 测试与离线预览会替换数据目录。"""

    def __init__(self, source_provider, dest_provider=default_dest, *,
                 interval_sec: float = BACKUP_INTERVAL_SEC, check_sec: float = BACKUP_CHECK_SEC):
        self._source = source_provider
        self._dest = dest_provider
        self.interval = timedelta(seconds=interval_sec)
        self.check_sec = check_sec
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self, now: datetime | None = None) -> Path | None:
        """到期就做一次快照并清理; 失败只记录, 不影响采集。"""
        now = now or datetime.now()
        dest = self._dest()
        try:
            existing = snapshots(dest)
            if existing and now - existing[0][0] < self.interval:
                return None
            path = create_snapshot(self._source(), dest, now)
            prune(dest, now)
        except Exception as exc:
            # 不只是 OSError: zipfile 也会抛 ValueError 等。漏掉的异常会让后台线程悄悄退出,
            # 状态接口却还显示没有错误。
            self.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[backup] 备份失败: {self.last_error}", flush=True)
            return None
        self.last_error = None
        return path

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="data-backup")
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.check_sec)

    def status(self) -> dict:
        dest = self._dest()
        try:
            existing = snapshots(dest)
        except OSError:
            existing = []
        return {"dir": str(dest), "count": len(existing),
                "latest": existing[0][1].name if existing else None, "error": self.last_error}


def main() -> int:
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    parser = argparse.ArgumentParser(description="立即给 data/ 做一次快照备份并按保留规则清理")
    parser.add_argument("--dest", default=None, help="备份目录(默认 FLOWSCOPE_BACKUP_DIR 或 backups/)")
    args = parser.parse_args()
    dest = args.dest or default_dest()
    path = create_snapshot(os.path.join(BASE_DIR, "data"), dest)
    removed = prune(dest)
    print(f"已备份到 {path}" + (f", 清理旧快照 {len(removed)} 个" if removed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
