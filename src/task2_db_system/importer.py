"""把任务1 的历史文本日志导入 ``log_event`` 表。

任务1 的日志是 ``logging`` 的终端格式：``2026-11-27 10:00:00 INFO    监听 ...``，
早期手工保存的文件可能没有日期、只有 ``HH:MM:SS``。这里对两种格式都做兼容：
缺日期时用文件最后修改日期补齐，无法识别的行按 INFO + 原文入库，保证"不丢行"。

用法（命令行界面里也有对应菜单项）::

    python -m src.task2_db_system.importer logs/server-20261127.log
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import CONTENT_LIMIT
from .db import connect, init_db, now_text, transaction
from .recorder import classify_text
from .repository import Repository

LOGGER = logging.getLogger("task2.importer")

# 兼容 `2026-11-27 10:00:00,123 INFO    ...` 与 `10:00:00 WARNING ...` 两种前缀。
LINE_RE = re.compile(
    r"^(?:(?P<date>\d{4}-\d{2}-\d{2})[ T])?(?P<time>\d{2}:\d{2}:\d{2})(?:[.,]\d+)?\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<message>.*)$"
)
LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def parse_line(line: str, default_date: str) -> Optional[Dict[str, str]]:
    """解析一行日志；空行返回 ``None``，无法识别时按 INFO 原文兜底。"""
    text = line.rstrip("\r\n")
    if not text.strip():
        return None
    match = LINE_RE.match(text)
    if not match:
        return {
            "ts": default_date,
            "level": "INFO",
            "message": text.strip()[:CONTENT_LIMIT],
        }
    date = match.group("date") or default_date
    return {
        "ts": "{0} {1}".format(date, match.group("time")),
        "level": match.group("level"),
        "message": match.group("message").strip()[:CONTENT_LIMIT],
    }


def import_log_file(
    conn: sqlite3.Connection,
    path: str | Path,
    instance_id: int = 0,
    chunk_size: int = 200,
) -> Dict[str, object]:
    """把 ``path`` 指向的日志文件批量导入，返回统计信息（行数/入库条数/时间范围）。"""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError("日志文件不存在: {0}".format(source))
    default_date = time.strftime("%Y-%m-%d", time.localtime(source.stat().st_mtime))
    repo = Repository(conn)
    if not instance_id:
        instance_id = repo.ensure_instance(
            "历史日志导入", host="", port=0, protocol="tcp", started_at=now_text()
        )

    imported = 0
    skipped = 0
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    batch: List[Tuple[object, ...]] = []
    with source.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            parsed = parse_line(raw, default_date)
            if parsed is None:
                skipped += 1
                continue
            levelno = LEVEL_MAP.get(parsed["level"], logging.INFO)
            guessed = classify_text(parsed["message"], levelno)
            batch.append(
                (
                    instance_id,
                    parsed["ts"],
                    parsed["level"],
                    "task1.import",
                    guessed["event_type"],
                    "",
                    guessed["actor"],
                    parsed["message"],
                    "",
                    "import",
                )
            )
            first_ts = first_ts or parsed["ts"]
            last_ts = parsed["ts"]
            imported += 1
            if len(batch) >= chunk_size:
                _insert(conn, batch)
                batch = []
    if batch:
        _insert(conn, batch)
    return {
        "file": str(source),
        "imported": imported,
        "skipped": skipped,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "instance_id": instance_id,
    }


def _insert(conn: sqlite3.Connection, rows: List[Tuple[object, ...]]) -> None:
    """单事务写一批日志，失败整批回滚，不留下半截数据。"""
    sql = (
        "INSERT INTO log_event"
        " (instance_id, ts, level, logger, event_type, room_name, actor, message, detail, thread)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    with transaction(conn):
        conn.executemany(sql, rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把任务1 的文本日志导入审计数据库")
    parser.add_argument("logfile", help="日志文件路径")
    parser.add_argument("--db", default=None, help="数据库路径（默认 data/chat_ops.db）")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    conn = connect(init_db(args.db))
    try:
        result = import_log_file(conn, args.logfile)
    finally:
        conn.close()
    print(
        "已导入 {imported} 行（跳过空行 {skipped} 行），时间范围 {first_ts} ~ {last_ts}".format(**result)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
