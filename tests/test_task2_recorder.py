from __future__ import annotations

import logging
import time
import unittest
from types import SimpleNamespace

from src.task2_db_system.db import connect
from src.task2_db_system.recorder import AuditRecorder, classify_text
from tests.test_task2_db import DATA_DIR, cleanup_sqlite, temp_db_path


def make_room(room_id: int = 1, name: str = "大厅", port: int = 9101) -> SimpleNamespace:
    return SimpleNamespace(
        room_id=room_id,
        name=name,
        host="127.0.0.1",
        port=port,
        created_by="服务器",
        created_at=time.time(),
        messages=[],
    )


def make_message(
    text: str,
    kind: str = "system",
    sender: str = "",
    target: str = "",
    event: str = "",
) -> SimpleNamespace:
    data = {"event": event} if event else {}
    return SimpleNamespace(
        type=kind, sender=sender, text=text, target=target, ts=time.time(), data=data
    )


class RecorderTests(unittest.TestCase):
    """写入端：日志接入、操作/消息入库、降级与收尾。"""

    def setUp(self) -> None:
        self.db_path = temp_db_path("audit")
        self.addCleanup(cleanup_sqlite, self.db_path)
        self.recorder = AuditRecorder(
            db_path=self.db_path,
            instance_name="测试服务器",
            host="127.0.0.1",
            port=9101,
            flush_interval=0.05,
            batch_size=20,
        )
        self.addCleanup(self.recorder.stop)

    def _rows(self, sql: str, *params: object) -> list:
        conn = connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_create_and_close_instance(self) -> None:
        self.assertTrue(self.recorder.start())
        self.assertIsNotNone(self.recorder.instance_id)
        self.recorder.record_server_started()
        self.recorder.stop()

        rows = self._rows("SELECT host, port, stopped_at FROM server_instance")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["host"], "127.0.0.1")
        self.assertEqual(rows[0]["port"], 9101)
        self.assertIsNotNone(rows[0]["stopped_at"])
        actions = [row["action"] for row in self._rows("SELECT action FROM operation")]
        self.assertIn("START", actions)

    def test_logging_pipeline_classifies_records(self) -> None:
        self.recorder.start()
        logger = logging.getLogger("unittest.audit")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        self.recorder.attach_logging(logger.name, logging.DEBUG)
        logger.info("127.0.0.1:5000 以昵称 小明 加入")
        logger.warning("[踢出] 小明（管理员操作）")
        logger.error("握手失败：对方拒绝连接")
        self.recorder.detach_logging()
        self.recorder.stop()

        rows = self._rows("SELECT level, event_type, actor, message FROM log_event ORDER BY id")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["event_type"], "加入")
        self.assertEqual(rows[0]["actor"], "小明")
        self.assertEqual(rows[0]["level"], "INFO")
        self.assertEqual(rows[1]["event_type"], "踢出")
        self.assertEqual(rows[1]["actor"], "小明")
        self.assertEqual(rows[2]["event_type"], "错误")
        self.assertTrue(rows[2]["message"].startswith("握手失败"))

    def test_room_messages_and_sessions(self) -> None:
        self.recorder.start()
        room = make_room()
        self.recorder.record_room_created(room)
        self.recorder.record_operation("KICK", actor="管理员", target="小明", room_name="大厅")
        self.recorder.record_room_message(room, make_message("小明 加入了聊天室", event="join", sender="小明"))
        self.recorder.record_room_message(
            room, make_message("大家好", kind="chat", sender="小明")
        )
        self.recorder.record_room_message(room, make_message("小明 离开了聊天室", event="leave", sender="小明"))
        self.recorder.stop()

        rooms = self._rows("SELECT name, hub_room_id FROM chat_room")
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["name"], "大厅")
        self.assertEqual(rooms[0]["hub_room_id"], 1)

        messages = self._rows("SELECT kind, sender, content FROM chat_message ORDER BY id")
        self.assertEqual([row["kind"] for row in messages], ["system", "chat", "system"])
        self.assertEqual(messages[1]["content"], "大家好")

        sessions = self._rows(
            "SELECT joined_at, left_at, leave_reason FROM user_session"
        )
        self.assertEqual(len(sessions), 1)
        self.assertIsNotNone(sessions[0]["joined_at"])
        self.assertIsNotNone(sessions[0]["left_at"])
        self.assertEqual(sessions[0]["leave_reason"], "正常离开")

        users = self._rows("SELECT nickname FROM chat_user")
        self.assertEqual([row["nickname"] for row in users], ["小明"])
        ops = self._rows("SELECT action, result FROM operation")
        self.assertIn(("KICK", "ok"), [(row["action"], row["result"]) for row in ops])

    def test_closed_room_records_reason(self) -> None:
        self.recorder.start()
        room = make_room(room_id=7, name="临时房间")
        self.recorder.record_room_created(room)
        self.recorder.record_room_closed(room, reason="人数不足")
        self.recorder.stop()
        rows = self._rows("SELECT closed_at, close_reason FROM chat_room WHERE hub_room_id = 7")
        self.assertIsNotNone(rows[0]["closed_at"])
        self.assertEqual(rows[0]["close_reason"], "人数不足")

    def test_unavailable_database_degrades(self) -> None:
        recorder = AuditRecorder(db_path=DATA_DIR, flush_interval=0.05)
        self.assertFalse(recorder.start())
        self.assertFalse(recorder.enabled)
        recorder.record_server_started()
        recorder.record_operation("BROADCAST")
        recorder.stop()
        self.assertEqual(recorder.written, 0)


class ClassifyTextTests(unittest.TestCase):
    def test_hints_and_actor(self) -> None:
        self.assertEqual(classify_text("小明 加入了聊天室")["event_type"], "加入")
        self.assertEqual(classify_text("[公告] 今晚维护")["event_type"], "公告")
        self.assertEqual(classify_text("普通一条日志")["event_type"], "其他")
        self.assertEqual(classify_text("普通一条日志", logging.ERROR)["event_type"], "错误")
        self.assertEqual(classify_text("127.0.0.1:1 以昵称 小红 加入")["actor"], "小红")


if __name__ == "__main__":
    unittest.main()
