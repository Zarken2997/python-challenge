"""参数化数据访问层：浏览、排序、筛选、分页与增删改查。

这一层是任务2 的"功能核心"：界面和命令行都只调用这里的方法，
不允许自己拼 SQL。这样排序字段、LIKE 转义、分页边界只实现一次，
也保证所有用户输入都走参数绑定，而不是字符串拼接。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from .config import (
    ACTIONS,
    EVENT_TYPES,
    LOG_LEVELS,
    MAX_EXPORT_ROWS,
    MESSAGE_KINDS,
    PAGE_SIZE,
    RESULT_LABELS,
    SEVERITIES,
    STATUSES,
    normalize_choice,
)
from .db import now_text, transaction

# 每个列表允许的排序字段。UI 传来的列名必须命中这里，否则拒绝，
# 避免把任意字符串拼进 ORDER BY（SQL 注入最常见的入口之一）。
SORTABLE_LOGS = {"id": "id", "ts": "ts", "level": "level", "event_type": "event_type",
                 "room_name": "room_name", "actor": "actor"}
SORTABLE_OPERATIONS = {"id": "id", "ts": "ts", "action": "action", "actor": "actor",
                       "room_name": "room_name", "result": "result"}
SORTABLE_MESSAGES = {"id": "id", "ts": "ts", "kind": "kind", "sender": "sender",
                     "room_name": "room_name", "content_len": "content_len"}
SORTABLE_INCIDENTS = {"id": "id", "created_at": "created_at", "updated_at": "updated_at",
                      "severity": "severity", "status": "status", "title": "title"}

MAX_PAGE_SIZE = 500


def _like(value: str) -> str:
    """把关键字转成 LIKE 模式，并转义 ``%``、``_``、``\\``。

    用户搜 "100%" 时应该当字面量，而不是通配符；SQL 里配套 ``ESCAPE '\\'``。
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _bound_start(value: str) -> str:
    """把 ``YYYY-MM-DD`` 补成当天 00:00:00；空串返回空串。"""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) == 10:
        return text + " 00:00:00"
    if len(text) < 10:
        raise ValueError(f"开始时间格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS，收到: {value!r}")
    return text


def _bound_end(value: str) -> str:
    """把 ``YYYY-MM-DD`` 补成当天 23:59:59，保证"截止到某天"包含当天。"""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) == 10:
        return text + " 23:59:59"
    if len(text) < 10:
        raise ValueError(f"结束时间格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS，收到: {value!r}")
    return text


def _order(sort: str, desc: bool, allowed: dict[str, str]) -> str:
    """生成 ORDER BY；字段不在白名单里直接报错，绝不静默放过。"""
    column = allowed.get(sort)
    if column is None:
        raise ValueError(f"不支持按 {sort!r} 排序，可选: {', '.join(sorted(allowed))}")
    return f"ORDER BY {column} {'DESC' if desc else 'ASC'}"


def _page_args(page: int, page_size: int) -> tuple[int, int, int]:
    """规范化分页参数，返回 ``(page, page_size, offset)``。"""
    try:
        page = int(page)
        page_size = int(page_size)
    except (TypeError, ValueError) as exc:
        raise ValueError("页码和每页条数必须是整数") from exc
    page = max(1, page)
    page_size = max(1, min(MAX_PAGE_SIZE, page_size))
    return page, page_size, (page - 1) * page_size


def as_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    """``sqlite3.Row`` → 普通 dict；``None`` 原样返回。"""
    return dict(row) if row is not None else None  # type: ignore[return-value]


class Repository:
    """所有查询与人工维护操作。一个实例持有一个连接，不要跨线程共享。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------ 运行日志

    def list_logs(
        self,
        keyword: str = "",
        level: str = "",
        event_type: str = "",
        room: str = "",
        actor: str = "",
        start: str = "",
        end: str = "",
        instance_id: int = 0,
        sort: str = "ts",
        desc: bool = True,
        page: int = 1,
        page_size: int = PAGE_SIZE,
    ) -> dict[str, Any]:
        """按关键字/级别/类型/房间/用户/时间段筛选运行日志，返回分页结果。"""
        level = normalize_choice(level, LOG_LEVELS, "级别")
        event_type = normalize_choice(event_type, EVENT_TYPES, "事件类型")
        clauses: list[str] = []
        params: list[Any] = []
        if keyword.strip():
            pattern = _like(keyword.strip())
            clauses.append(
                "(message LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\'"
                " OR actor LIKE ? ESCAPE '\\' OR room_name LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern] * 4)
        if level:
            clauses.append("level = ?")
            params.append(level)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if room.strip():
            clauses.append("room_name LIKE ? ESCAPE '\\'")
            params.append(_like(room.strip()))
        if actor.strip():
            clauses.append("actor LIKE ? ESCAPE '\\'")
            params.append(_like(actor.strip()))
        if instance_id:
            clauses.append("instance_id = ?")
            params.append(int(instance_id))
        start_text = _bound_start(start)
        if start_text:
            clauses.append("ts >= ?")
            params.append(start_text)
        end_text = _bound_end(end)
        if end_text:
            clauses.append("ts <= ?")
            params.append(end_text)
        return self._paged(
            "log_event",
            "SELECT * FROM log_event",
            clauses,
            params,
            _order(sort, desc, SORTABLE_LOGS),
            page,
            page_size,
        )

    def get_log(self, log_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM log_event WHERE id = ?", (int(log_id),)).fetchone()
        return as_dict(row)

    def recent_errors(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM log_event WHERE level IN ('ERROR', 'CRITICAL') ORDER BY ts DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ 聊天记录

    def list_messages(
        self,
        keyword: str = "",
        kind: str = "",
        room: str = "",
        sender: str = "",
        start: str = "",
        end: str = "",
        sort: str = "ts",
        desc: bool = True,
        page: int = 1,
        page_size: int = PAGE_SIZE,
    ) -> dict[str, Any]:
        """聊天正文查询（用户明确要求正文入库，这里提供按内容关键字检索）。"""
        kind = normalize_choice(kind, MESSAGE_KINDS, "消息类型")
        clauses: list[str] = []
        params: list[Any] = []
        if keyword.strip():
            pattern = _like(keyword.strip())
            clauses.append("(m.content LIKE ? ESCAPE '\\' OR m.sender LIKE ? ESCAPE '\\')")
            params.extend([pattern, pattern])
        if kind:
            clauses.append("m.kind = ?")
            params.append(kind)
        if sender.strip():
            clauses.append("m.sender LIKE ? ESCAPE '\\'")
            params.append(_like(sender.strip()))
        if room.strip():
            clauses.append("r.name LIKE ? ESCAPE '\\'")
            params.append(_like(room.strip()))
        start_text = _bound_start(start)
        if start_text:
            clauses.append("m.ts >= ?")
            params.append(start_text)
        end_text = _bound_end(end)
        if end_text:
            clauses.append("m.ts <= ?")
            params.append(end_text)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        base = (
            "SELECT m.*, COALESCE(r.name, '') AS room_name"
            " FROM chat_message m LEFT JOIN chat_room r ON r.id = m.room_id"
        )
        total = int(self.conn.execute(f"SELECT COUNT(*) AS n FROM ({base}{where})", params).fetchone()["n"])
        page, page_size, offset = _page_args(page, page_size)
        rows = self.conn.execute(
            f"{base}{where} {_order(sort, desc, SORTABLE_MESSAGES)} LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
        return {"rows": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    # ------------------------------------------------------------ 操作审计

    def list_operations(
        self,
        keyword: str = "",
        action: str = "",
        actor: str = "",
        result: str = "",
        start: str = "",
        end: str = "",
        sort: str = "ts",
        desc: bool = True,
        page: int = 1,
        page_size: int = PAGE_SIZE,
    ) -> dict[str, Any]:
        action = normalize_choice(action, ACTIONS, "操作类型")
        if result:
            result = normalize_choice(result, ("ok", "failed"), "结果")
        clauses: list[str] = []
        params: list[Any] = []
        if keyword.strip():
            pattern = _like(keyword.strip())
            clauses.append(
                "(target LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\' OR room_name LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern] * 3)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if actor.strip():
            clauses.append("actor LIKE ? ESCAPE '\\'")
            params.append(_like(actor.strip()))
        if result:
            clauses.append("result = ?")
            params.append(result)
        start_text = _bound_start(start)
        if start_text:
            clauses.append("ts >= ?")
            params.append(start_text)
        end_text = _bound_end(end)
        if end_text:
            clauses.append("ts <= ?")
            params.append(end_text)
        return self._paged(
            "operation",
            "SELECT * FROM operation",
            clauses,
            params,
            _order(sort, desc, SORTABLE_OPERATIONS),
            page,
            page_size,
        )

    def insert_operation(
        self,
        action: str,
        actor: str = "服务器",
        target: str = "",
        room_name: str = "",
        result: str = "ok",
        detail: str = "",
        instance_id: int | None = None,
        ts: str = "",
    ) -> int:
        """写一条审计记录（导入、清理、人工处置都用它留痕）。"""
        action = normalize_choice(action, ACTIONS, "操作类型")
        result = normalize_choice(result, tuple(RESULT_LABELS), "结果")
        cursor = self.conn.execute(
            "INSERT INTO operation (instance_id, ts, action, actor, target, room_name, result, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (instance_id, ts or now_text(), action, actor, target, room_name, result, detail),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    # ------------------------------------------------------------ 事故记录

    def list_incidents(
        self,
        keyword: str = "",
        status: str = "",
        severity: str = "",
        sort: str = "updated_at",
        desc: bool = True,
        page: int = 1,
        page_size: int = PAGE_SIZE,
    ) -> dict[str, Any]:
        status = normalize_choice(status, STATUSES, "状态")
        severity = normalize_choice(severity, SEVERITIES, "严重级别")
        clauses: list[str] = []
        params: list[Any] = []
        if keyword.strip():
            pattern = _like(keyword.strip())
            clauses.append(
                "(title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\'"
                " OR assignee LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern] * 3)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        return self._paged(
            "incident",
            "SELECT * FROM incident",
            clauses,
            params,
            _order(sort, desc, SORTABLE_INCIDENTS),
            page,
            page_size,
        )

    def get_incident(self, incident_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM incident WHERE id = ?", (int(incident_id),)).fetchone()
        return as_dict(row)

    def create_incident(
        self,
        title: str,
        severity: str = "中",
        status: str = "待处理",
        assignee: str = "",
        description: str = "",
        log_event_id: int | None = None,
        instance_id: int | None = None,
    ) -> int:
        """新建事故记录；标题必填，级别/状态必须是枚举值，关联日志必须存在。"""
        title = (title or "").strip()
        if not title:
            raise ValueError("事故标题不能为空")
        severity = normalize_choice(severity, SEVERITIES, "严重级别") or "中"
        status = normalize_choice(status, STATUSES, "状态") or "待处理"
        if log_event_id is not None and self.get_log(int(log_event_id)) is None:
            raise ValueError(f"关联的运行日志不存在: {log_event_id}")
        stamp = now_text()
        resolved = stamp if status == "已关闭" else None
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO incident (instance_id, title, severity, status, assignee, description,"
                " log_event_id, created_at, updated_at, resolved_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    instance_id,
                    title,
                    severity,
                    status,
                    assignee.strip(),
                    description.strip(),
                    log_event_id,
                    stamp,
                    stamp,
                    resolved,
                ),
            )
            incident_id = int(cursor.lastrowid)
        return incident_id

    def update_incident(self, incident_id: int, **fields: Any) -> bool:
        """修改事故记录；只接受白名单字段，状态变化时自动维护 resolved_at。"""
        current = self.get_incident(incident_id)
        if current is None:
            return False
        allowed = {"title", "severity", "status", "assignee", "description", "log_event_id"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不支持修改字段: {', '.join(sorted(unknown))}")
        merged = {key: current[key] for key in allowed}
        for key, value in fields.items():
            if key == "title":
                value = str(value or "").strip()
                if not value:
                    raise ValueError("事故标题不能为空")
            elif key == "severity":
                value = normalize_choice(str(value), SEVERITIES, "严重级别") or current["severity"]
            elif key == "status":
                value = normalize_choice(str(value), STATUSES, "状态") or current["status"]
            elif key == "assignee":
                value = str(value or "").strip()
            elif key == "description":
                value = str(value or "").strip()
            elif key == "log_event_id":
                value = None if value in (None, "", 0, "0") else int(value)
                if value is not None and self.get_log(value) is None:
                    raise ValueError(f"关联的运行日志不存在: {value}")
            merged[key] = value
        if merged["status"] == "已关闭":
            resolved = current["resolved_at"] or now_text()
        else:
            resolved = None
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE incident SET title = ?, severity = ?, status = ?, assignee = ?,"
                " description = ?, log_event_id = ?, updated_at = ?, resolved_at = ? WHERE id = ?",
                (
                    merged["title"],
                    merged["severity"],
                    merged["status"],
                    merged["assignee"],
                    merged["description"],
                    merged["log_event_id"],
                    now_text(),
                    resolved,
                    int(incident_id),
                ),
            )
        return True

    def delete_incident(self, incident_id: int) -> bool:
        with transaction(self.conn):
            cursor = self.conn.execute("DELETE FROM incident WHERE id = ?", (int(incident_id),))
        return cursor.rowcount > 0

    # ------------------------------------------------------------ 房间 / 用户

    def list_rooms(self, instance_id: int = 0, keyword: str = "") -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if instance_id:
            clauses.append("r.instance_id = ?")
            params.append(int(instance_id))
        if keyword.strip():
            clauses.append("r.name LIKE ? ESCAPE '\\'")
            params.append(_like(keyword.strip()))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            "SELECT r.*,"
            " (SELECT COUNT(*) FROM user_session s WHERE s.room_id = r.id) AS session_count,"
            " (SELECT COUNT(*) FROM chat_message m WHERE m.room_id = r.id) AS message_count"
            f" FROM chat_room r{where} ORDER BY r.created_at DESC LIMIT 500",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_users(self, keyword: str = "") -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if keyword.strip():
            where = " WHERE u.nickname LIKE ? ESCAPE '\\'"
            params.append(_like(keyword.strip()))
        rows = self.conn.execute(
            "SELECT u.*,"
            " (SELECT COUNT(*) FROM user_session s WHERE s.user_id = u.id) AS session_count,"
            " (SELECT COUNT(*) FROM chat_message m WHERE m.sender = u.nickname) AS message_count"
            f" FROM chat_user u{where} ORDER BY u.last_seen DESC LIMIT 500",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_sessions(self, active_only: bool = True, limit: int = 300) -> list[dict[str, Any]]:
        where = " WHERE s.left_at IS NULL" if active_only else ""
        rows = self.conn.execute(
            "SELECT s.*, r.name AS room_name, u.nickname"
            " FROM user_session s"
            " JOIN chat_room r ON r.id = s.room_id"
            " JOIN chat_user u ON u.id = s.user_id"
            f"{where} ORDER BY s.joined_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_user_note(self, nickname: str, note: str) -> bool:
        with transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE chat_user SET note = ? WHERE nickname = ?", (str(note or ""), nickname)
            )
        return cursor.rowcount > 0

    def list_instances(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM server_instance ORDER BY started_at DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ 写线程用的辅助

    def ensure_instance(
        self,
        name: str,
        host: str,
        port: int,
        protocol: str = "tcp",
        started_at: str = "",
    ) -> int:
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO server_instance (name, host, port, protocol, started_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, host, int(port), protocol, started_at or now_text()),
            )
        return int(cursor.lastrowid)

    def stop_instance(self, instance_id: int, stopped_at: str = "") -> None:
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE server_instance SET stopped_at = ? WHERE id = ? AND stopped_at IS NULL",
                (stopped_at or now_text(), int(instance_id)),
            )

    def ensure_room(
        self,
        instance_id: int,
        hub_room_id: int,
        name: str,
        host: str,
        port: int,
        created_by: str,
        created_at: str = "",
    ) -> int:
        """按 ``(instance_id, hub_room_id)`` 幂等写入房间，返回数据库主键。"""
        row = self.conn.execute(
            "SELECT id FROM chat_room WHERE instance_id = ? AND hub_room_id = ?",
            (int(instance_id), int(hub_room_id)),
        ).fetchone()
        if row is not None:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE chat_room SET name = ?, host = ?, port = ? WHERE id = ?",
                    (name, host, int(port), int(row["id"])),
                )
            return int(row["id"])
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO chat_room (instance_id, hub_room_id, name, host, port, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (int(instance_id), int(hub_room_id), name, host, int(port), created_by,
                 created_at or now_text()),
            )
        return int(cursor.lastrowid)

    def close_room(self, instance_id: int, hub_room_id: int, reason: str = "") -> bool:
        with transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE chat_room SET closed_at = ?, close_reason = ?"
                " WHERE instance_id = ? AND hub_room_id = ? AND closed_at IS NULL",
                (now_text(), reason, int(instance_id), int(hub_room_id)),
            )
        return cursor.rowcount > 0

    def ensure_user(self, nickname: str, seen_at: str = "") -> int:
        """按昵称幂等写入用户：没有就建，有就只更新 ``last_seen``。"""
        stamp = seen_at or now_text()
        row = self.conn.execute(
            "SELECT id FROM chat_user WHERE nickname = ?", (nickname,)
        ).fetchone()
        if row is not None:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE chat_user SET last_seen = ? WHERE id = ?", (stamp, int(row["id"]))
                )
            return int(row["id"])
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO chat_user (nickname, first_seen, last_seen) VALUES (?, ?, ?)",
                (nickname, stamp, stamp),
            )
        return int(cursor.lastrowid)

    def open_session(
        self,
        instance_id: int,
        room_id: int,
        user_id: int,
        addr: str = "",
        joined_at: str = "",
    ) -> int:
        with transaction(self.conn):
            cursor = self.conn.execute(
                "INSERT INTO user_session (instance_id, room_id, user_id, addr, joined_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (int(instance_id), int(room_id), int(user_id), addr, joined_at or now_text()),
            )
        return int(cursor.lastrowid)

    def close_session(self, session_id: int, reason: str = "") -> bool:
        with transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE user_session SET left_at = ?, leave_reason = ?"
                " WHERE id = ? AND left_at IS NULL",
                (now_text(), reason, int(session_id)),
            )
        return cursor.rowcount > 0

    def close_user_sessions(self, user_id: int, room_id: int, reason: str = "") -> int:
        """兜底：用户离开时把该房间内所有未闭合会话都关掉（防止断线漏记）。"""
        with transaction(self.conn):
            cursor = self.conn.execute(
                "UPDATE user_session SET left_at = ?, leave_reason = ?"
                " WHERE user_id = ? AND room_id = ? AND left_at IS NULL",
                (now_text(), reason, int(user_id), int(room_id)),
            )
        return cursor.rowcount

    def purge_logs(self, before: str) -> int:
        """删除 ``before`` 之前的运行日志；调用方必须先导出并写审计记录。

        这里刻意拒绝只写日期的输入：日志量按天算很大，误删一天的代价太高，
        必须让调用者明确到时分秒。
        """
        raw = (before or "").strip()
        if len(raw) <= 10 or " " not in raw:
            raise ValueError("清理日志必须给出 YYYY-MM-DD HH:MM:SS 形式的截止时间")
        before_text = _bound_start(raw)
        with transaction(self.conn):
            cursor = self.conn.execute("DELETE FROM log_event WHERE ts < ?", (before_text,))
        return int(cursor.rowcount)

    # ------------------------------------------------------------ 内部

    def _paged(
        self,
        table: str,
        select_sql: str,
        clauses: Iterable[str],
        params: list[Any],
        order_sql: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        where = (" WHERE " + " AND ".join(clauses)) if list(clauses) else ""
        params = list(params)
        total = int(
            self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}{where}", params).fetchone()["n"]
        )
        page, page_size, offset = _page_args(page, page_size)
        rows = self.conn.execute(
            f"{select_sql}{where} {order_sql} LIMIT ? OFFSET ?", params + [page_size, offset]
        ).fetchall()
        return {
            "rows": [dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
        }

    def export_logs(self, **filters: Any) -> list[dict[str, Any]]:
        """导出用：复用筛选逻辑，但一次取到上限条数（不分页）。"""
        filters = dict(filters)
        filters["page"] = 1
        filters["page_size"] = MAX_EXPORT_ROWS
        return self.list_logs(**filters)["rows"]
