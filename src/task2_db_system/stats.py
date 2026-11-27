"""统计查询：给界面顶部的"概览"和命令行 `stats` 子命令提供数字。

这些 SQL 都很短，但口径必须统一，所以集中在这里，避免界面、
命令行、报告三处各写一遍、各算一套。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .db import TIME_FORMAT, now_text


def _scope(instance_id: int = 0) -> tuple[str, list[object]]:
    """生成可选的实例过滤条件；``0`` 表示全部实例。"""
    if instance_id:
        return " WHERE instance_id = ?", [int(instance_id)]
    return "", []


def _scalar(conn: sqlite3.Connection, sql: str, params: list[object] | tuple[object, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def overview(conn: sqlite3.Connection, instance_id: int = 0) -> dict[str, object]:
    """一次汇总出界面概览需要的所有计数。"""
    where, params = _scope(instance_id)
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN level = 'ERROR' THEN 1 ELSE 0 END) AS errors,"
        " SUM(CASE WHEN level = 'WARNING' THEN 1 ELSE 0 END) AS warnings,"
        " MIN(ts) AS first_ts, MAX(ts) AS last_ts"
        f" FROM log_event{where}",
        params,
    ).fetchone()
    return {
        "logs_total": int(row["total"] or 0),
        "logs_error": int(row["errors"] or 0),
        "logs_warning": int(row["warnings"] or 0),
        "first_log_ts": row["first_ts"] or "",
        "last_log_ts": row["last_ts"] or "",
        "messages_total": _scalar(conn, f"SELECT COUNT(*) FROM chat_message{where}", params),
        "operations_total": _scalar(conn, f"SELECT COUNT(*) FROM operation{where}", params),
        "incidents_open": _scalar(conn, "SELECT COUNT(*) FROM incident WHERE status <> '已关闭'"),
        "incidents_total": _scalar(conn, "SELECT COUNT(*) FROM incident"),
        "rooms_total": _scalar(conn, "SELECT COUNT(*) FROM chat_room"),
        "rooms_active": _scalar(conn, "SELECT COUNT(*) FROM chat_room WHERE closed_at IS NULL"),
        "users_total": _scalar(conn, "SELECT COUNT(*) FROM chat_user"),
        "sessions_active": _scalar(conn, "SELECT COUNT(*) FROM user_session WHERE left_at IS NULL"),
    }


def level_counts(conn: sqlite3.Connection, instance_id: int = 0) -> list[dict[str, object]]:
    where, params = _scope(instance_id)
    rows = conn.execute(
        f"SELECT level, COUNT(*) AS n FROM log_event{where} GROUP BY level ORDER BY n DESC", params
    ).fetchall()
    return [{"level": row["level"], "count": int(row["n"])} for row in rows]


def event_type_counts(
    conn: sqlite3.Connection, limit: int = 10, instance_id: int = 0
) -> list[dict[str, object]]:
    where, params = _scope(instance_id)
    rows = conn.execute(
        "SELECT event_type, COUNT(*) AS n FROM log_event"
        f"{where} GROUP BY event_type ORDER BY n DESC LIMIT ?",
        list(params) + [max(1, int(limit))],
    ).fetchall()
    return [{"event_type": row["event_type"], "count": int(row["n"])} for row in rows]


def action_counts(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, object]]:
    """按动作聚合管理操作次数（成功/失败合在一起），比逐个 result 分组更直观。"""
    rows = conn.execute(
        "SELECT action, COUNT(*) AS n FROM operation GROUP BY action ORDER BY n DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [{"action": row["action"], "count": int(row["n"])} for row in rows]


def hourly_counts(
    conn: sqlite3.Connection, hours: int = 24, instance_id: int = 0, now: str = ""
) -> list[dict[str, object]]:
    """最近 N 小时每小时的日志条数，没有数据的小时补 0，方便画趋势图。"""
    end = datetime.strptime(now or now_text(), TIME_FORMAT)
    start = end - timedelta(hours=max(1, int(hours)) - 1)
    start = start.replace(minute=0, second=0)
    where = " WHERE ts >= ?"
    params: list[object] = [start.strftime(TIME_FORMAT)]
    if instance_id:
        where += " AND instance_id = ?"
        params.append(int(instance_id))
    rows = conn.execute(
        f"SELECT substr(ts, 1, 13) AS hour, COUNT(*) AS n FROM log_event{where} GROUP BY hour",
        params,
    ).fetchall()
    found = {str(row["hour"]): int(row["n"]) for row in rows}
    result: list[dict[str, object]] = []
    cursor = start
    while cursor <= end:
        key = cursor.strftime("%Y-%m-%d %H")
        result.append({"hour": key, "count": found.get(key, 0)})
        cursor += timedelta(hours=1)
    return result


def top_rooms(conn: sqlite3.Connection, limit: int = 8) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT COALESCE(r.name, '') AS room_name, COUNT(*) AS n"
        " FROM chat_message m LEFT JOIN chat_room r ON r.id = m.room_id"
        " GROUP BY m.room_id ORDER BY n DESC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [{"room_name": row["room_name"], "count": int(row["n"])} for row in rows]


def incident_summary(conn: sqlite3.Connection) -> dict[str, list[dict[str, object]]]:
    by_status = conn.execute(
        "SELECT status, COUNT(*) AS n FROM incident GROUP BY status ORDER BY n DESC"
    ).fetchall()
    by_severity = conn.execute(
        "SELECT severity, COUNT(*) AS n FROM incident GROUP BY severity ORDER BY n DESC"
    ).fetchall()
    return {
        "by_status": [{"status": row["status"], "count": int(row["n"])} for row in by_status],
        "by_severity": [{"severity": row["severity"], "count": int(row["n"])} for row in by_severity],
    }


def daily_counts(conn: sqlite3.Connection, days: int = 7, instance_id: int = 0) -> list[dict[str, object]]:
    """最近 N 天每天的日志条数（报告里做"日志量按天分布"用）。"""
    where = ""
    params: list[object] = []
    if instance_id:
        where = " WHERE instance_id = ?"
        params.append(int(instance_id))
    rows = conn.execute(
        "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n FROM log_event"
        f"{where} GROUP BY day ORDER BY day DESC LIMIT ?",
        params + [max(1, int(days))],
    ).fetchall()
    return [{"day": row["day"], "count": int(row["n"])} for row in reversed(rows)]
