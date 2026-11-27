"""任务2 数据库层测试：建表、约束、筛选、分页、事故 CRUD、清理规则。"""

from __future__ import annotations

import sqlite3
import unittest
import uuid
from pathlib import Path

from src.task2_db_system.db import connect, init_db, now_text, open_db, table_counts
from src.task2_db_system.repository import Repository

# 沙箱允许写 data/ 根目录，但拒绝在新建子目录里落库（见 DEVLOG），
# 所以测试库直接用 data/ 下的唯一文件名，用完删除。
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"


def temp_db_path(prefix: str = "t2db") -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "{0}-{1}.db".format(prefix, uuid.uuid4().hex[:10])


def cleanup_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()


class DatabaseTestCase(unittest.TestCase):
    """每个用例一个独立临时库，结束后关闭连接再删文件（Windows 需要）。"""

    def setUp(self) -> None:
        self.db_path = temp_db_path()
        self.addCleanup(cleanup_sqlite, self.db_path)
        self.db_path = init_db(self.db_path)
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.repo = Repository(self.conn)

    def _instance(self) -> int:
        return self.repo.ensure_instance(
            "测试服务器", "127.0.0.1", 9000, "tcp", "2026-11-27 09:00:00"
        )

    def _log(self, message: str = "hello", level: str = "INFO", ts: str = "") -> None:
        self.conn.execute(
            "INSERT INTO log_event (instance_id, ts, level, logger, event_type, actor, message)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self._instance(), ts or now_text(), level, "task1.server", "其他", "", message),
        )
        self.conn.commit()


class SchemaTests(DatabaseTestCase):
    def test_init_is_idempotent_and_enables_wal(self) -> None:
        init_db(self.db_path)
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        counts = table_counts(self.conn)
        for table in (
            "server_instance",
            "chat_room",
            "chat_user",
            "user_session",
            "log_event",
            "operation",
            "chat_message",
            "incident",
        ):
            self.assertIn(table, counts)

    def test_open_db_creates_missing_file(self) -> None:
        fresh = temp_db_path("fresh")
        self.addCleanup(cleanup_sqlite, fresh)
        conn = open_db(fresh)
        try:
            self.assertTrue(fresh.exists())
            self.assertIn("log_event", table_counts(conn))
        finally:
            conn.close()

    def test_level_and_kind_constraints(self) -> None:
        instance_id = self._instance()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO log_event (ts, level, logger, event_type, message)"
                " VALUES (?, ?, ?, ?, ?)",
                (now_text(), "TRACE", "task1.server", "其他", "bad level"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO chat_message (ts, kind, sender, content, content_len)"
                " VALUES (?, ?, ?, ?, ?)",
                (now_text(), "broadcast", "a", "x", 1),
            )
        # 合法值可以写入（instance_id 可空，历史导入没有实例）
        self.conn.execute(
            "INSERT INTO log_event (ts, level, logger, event_type, message)"
            " VALUES (?, 'ERROR', 'task1.server', '错误', 'ok')",
            (now_text(),),
        )
        self.assertEqual(table_counts(self.conn)["server_instance"], 1)
        self.assertIsNotNone(instance_id)

    def test_user_nickname_unique(self) -> None:
        self.repo.ensure_user("小明", "2026-11-27 09:00:00")
        self.repo.ensure_user("小明", "2026-11-27 10:00:00")
        rows = self.conn.execute("SELECT COUNT(*) FROM chat_user").fetchone()[0]
        self.assertEqual(rows, 1)


class FilterTests(DatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._log("100% 完成", "INFO", "2026-11-27 08:00:00")
        self._log("握手失败", "ERROR", "2026-11-27 09:00:00")
        self._log("心跳超时", "WARNING", "2026-11-27 10:00:00")

    def test_keyword_escapes_like_wildcards(self) -> None:
        result = self.repo.list_logs(keyword="100%", start="", end="", page_size=10)
        self.assertEqual(len(result["rows"]), 1)
        # `%` 被当成普通字符，不会把全部记录都匹配出来
        self.assertEqual(len(self.repo.list_logs(keyword="%", page_size=10)["rows"]), 1)

    def test_level_and_date_range(self) -> None:
        errors = self.repo.list_logs(level="ERROR", page_size=10)["rows"]
        self.assertEqual([row["message"] for row in errors], ["握手失败"])
        ranged = self.repo.list_logs(start="2026-11-27", end="2026-11-27", page_size=10)["rows"]
        self.assertEqual(len(ranged), 3)
        none = self.repo.list_logs(start="2026-11-28", page_size=10)["rows"]
        self.assertEqual(none, [])

    def test_sort_whitelist_and_pagination(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.list_logs(sort="message; DROP TABLE log_event")
        page1 = self.repo.list_logs(page=1, page_size=2)
        page2 = self.repo.list_logs(page=2, page_size=2)
        self.assertEqual(page1["total"], 3)
        self.assertEqual(page1["pages"], 2)
        self.assertEqual(len(page1["rows"]), 2)
        self.assertEqual(len(page2["rows"]), 1)
        self.assertNotEqual(page1["rows"][0]["id"], page2["rows"][0]["id"])

    def test_ascending_sort(self) -> None:
        rows = self.repo.list_logs(sort="ts", desc=False, page_size=10)["rows"]
        self.assertEqual(rows[0]["ts"], "2026-11-27 08:00:00")
        self.assertEqual(rows[-1]["ts"], "2026-11-27 10:00:00")


class WriteTests(DatabaseTestCase):
    def test_message_and_operation_filters(self) -> None:
        instance_id = self._instance()
        room_id = self.repo.ensure_room(instance_id, 1, "大厅", "127.0.0.1", 9001, "管理员")
        self.repo.ensure_user("小明", "2026-11-27 09:00:00")
        self.conn.execute(
            "INSERT INTO chat_message (instance_id, room_id, ts, kind, sender, content, content_len)"
            " VALUES (?, ?, ?, 'chat', '小明', '晚上好', 3)",
            (instance_id, room_id, now_text()),
        )
        self.conn.commit()
        found = self.repo.list_messages(keyword="晚上", page_size=10)["rows"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["room_name"], "大厅")
        self.assertEqual(self.repo.list_messages(kind="private", page_size=10)["rows"], [])

        self.repo.insert_operation("KICK", actor="管理员", target="小明", result="ok")
        self.repo.insert_operation("KICK", actor="管理员", target="小红", result="failed")
        self.assertEqual(len(self.repo.list_operations(action="KICK")["rows"]), 2)
        self.assertEqual(len(self.repo.list_operations(result="failed")["rows"]), 1)
        with self.assertRaises(ValueError):
            self.repo.insert_operation("NOT_AN_ACTION")

    def test_incident_crud_and_validation(self) -> None:
        incident_id = self.repo.create_incident("磁盘告警", severity="高", assignee="管理员")
        self.assertIsNotNone(self.repo.get_incident(incident_id))
        with self.assertRaises(ValueError):
            self.repo.create_incident("x", severity="灾难")
        updated = self.repo.update_incident(incident_id, status="已关闭")
        self.assertTrue(updated)
        row = self.repo.get_incident(incident_id)
        self.assertEqual(row["status"], "已关闭")
        self.assertTrue(row["resolved_at"])
        self.assertTrue(self.repo.delete_incident(incident_id))
        self.assertIsNone(self.repo.get_incident(incident_id))
        self.assertFalse(self.repo.delete_incident(incident_id))

    def test_session_lifecycle(self) -> None:
        instance_id = self._instance()
        room_id = self.repo.ensure_room(instance_id, 1, "大厅", "127.0.0.1", 9001, "管理员")
        user_id = self.repo.ensure_user("小明", "2026-11-27 09:00:00")
        session_id = self.repo.open_session(
            instance_id, room_id, user_id, "127.0.0.1:50000", "2026-11-27 09:00:00"
        )
        self.assertEqual(len(self.repo.list_sessions()), 1)
        self.repo.close_session(session_id, "主动退出")
        self.assertEqual(self.repo.list_sessions(), [])
        self.assertEqual(len(self.repo.list_sessions(active_only=False)), 1)
        # 重新进一次会话，再用兜底方法按 (user, room) 关闭
        second = self.repo.open_session(instance_id, room_id, user_id, "127.0.0.1:50001")
        self.assertEqual(self.repo.close_user_sessions(user_id, room_id, "强制下线"), 1)
        self.assertEqual(self.repo.list_sessions(), [])
        self.assertGreater(second, 0)

    def test_purge_requires_full_timestamp(self) -> None:
        self._log("old", ts="2026-11-01 00:00:00")
        with self.assertRaises(ValueError):
            self.repo.purge_logs("2026-11-27")
        deleted = self.repo.purge_logs("2026-11-02 00:00:00")
        self.assertEqual(deleted, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log_event").fetchone()[0], 0)


class CountTests(DatabaseTestCase):
    def test_table_counts_include_empty_tables(self) -> None:
        counts = table_counts(self.conn)
        self.assertEqual(counts["log_event"], 0)
        self._log()
        self.assertEqual(table_counts(self.conn)["log_event"], 1)


if __name__ == "__main__":
    unittest.main()
