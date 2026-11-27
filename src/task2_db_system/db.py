"""SQLite 连接与建库：统一 PRAGMA、事务边界和初始化入口。

为什么单独一层：连接参数（WAL、外键、busy_timeout）如果散落在各个模块，
很容易出现"某个连接忘了开外键"的隐蔽 bug。所有模块都必须经过这里拿连接。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .config import DEFAULT_DB_PATH, SCHEMA_PATH

SCHEMA_VERSION = "1"

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
"""数据库里所有时间列的统一格式；字符串比较等价于时间比较。"""


def now_text(moment: float | None = None) -> str:
    """把时间戳转成 ``YYYY-MM-DD HH:MM:SS``；不传则取当前本地时间。"""
    if moment is None:
        moment = time.time()
    return datetime.fromtimestamp(float(moment)).strftime(TIME_FORMAT)


def connect(path: str | Path | None = None, readonly: bool = False) -> sqlite3.Connection:
    """打开数据库连接并设置统一 PRAGMA。

    ``readonly=True`` 用于只读巡查；写连接会自动建父目录并启用 WAL。
    WAL 让"服务器写日志"和"界面查日志"可以同时进行，读不阻塞写。
    """
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    if readonly:
        if not db_path.exists():
            raise FileNotFoundError(f"数据库不存在: {db_path}")
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: str | Path | None = None) -> Path:
    """建库/升级表结构；重复调用安全（所有 DDL 都是 ``IF NOT EXISTS``）。

    返回实际数据库路径，方便调用方打印"库建在哪里"。
    """
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def is_initialized(conn: sqlite3.Connection) -> bool:
    """判断当前文件是否已经有本系统的表（避免把空文件当成可用库）。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'log_event'"
    ).fetchone()
    return row is not None


def open_db(path: str | Path | None = None, create: bool = True) -> sqlite3.Connection:
    """打开业务库：不存在且 ``create=True`` 时先建库，否则直接报错。"""
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    if create and not db_path.exists():
        init_db(db_path)
    conn = connect(db_path)
    if not is_initialized(conn):
        conn.close()
        raise RuntimeError(f"不是本系统的数据库（缺少表结构）: {db_path}")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务：正常提交，异常回滚。

    批量写 200 条日志时，一条事务比 200 条自动提交快一个数量级，
    也保证"要么全写进去，要么一条都不写"，不会留下半批数据。
    """
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """各表行数，用于界面概览和测试断言。"""
    names = (
        "server_instance",
        "chat_room",
        "chat_user",
        "user_session",
        "log_event",
        "operation",
        "chat_message",
        "incident",
    )
    counts: dict[str, int] = {}
    for name in names:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()
        counts[name] = int(row["n"]) if row is not None else 0
    return counts
