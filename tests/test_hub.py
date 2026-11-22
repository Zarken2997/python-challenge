"""聊天室注册中心（hub）的集成测试：真实回环 socket，不依赖文件系统。

覆盖点：控制端口握手、房间创建/关闭、重名与上限、房间内正常聊天、
全服公告、开房申请审批、踢人、房间列表推送、在线用户汇总。
"""

from __future__ import annotations

import socket
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.task1_socket_chat.config import HubConfig  # noqa: E402
from src.task1_socket_chat.hub import ChatHub  # noqa: E402
from src.task1_socket_chat.protocol import (  # noqa: E402
    Message,
    read_message,
    send_message,
)


class HubTestBase(unittest.TestCase):
    """负责启动/关闭一个使用随机端口的注册中心。"""

    max_rooms = 5

    def setUp(self) -> None:
        self.hub = ChatHub(HubConfig(host="127.0.0.1", port=0, max_rooms=self.max_rooms))
        self.host, self.port = self.hub.start()
        self._socks: list[socket.socket] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for sock in self._socks:
            try:
                sock.close()
            except OSError:
                pass
        self.hub.shutdown()

    # ------------------------------------------------------------ 辅助方法

    def track(self, sock: socket.socket) -> socket.socket:
        self._socks.append(sock)
        return sock

    def open_control(self, name: str = "alice") -> tuple[socket.socket, Message]:
        """建立控制连接并完成 hello/welcome 握手。"""
        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.settimeout(5)
        self.track(sock)
        send_message(sock, Message(type="hello", sender=name))
        return sock, read_message(sock)

    def open_room(self, host: str, port: int, name: str = "bob") -> tuple[socket.socket, Message]:
        """进入某个聊天室并完成握手。"""
        sock = socket.create_connection((host, port), timeout=5)
        sock.settimeout(5)
        self.track(sock)
        send_message(sock, Message(type="hello", sender=name))
        return sock, read_message(sock)

    def read_until(self, sock: socket.socket, kind: str, limit: int = 10) -> Message:
        """一路读到指定类型的消息，跳过中间的房间列表/心跳等通知。"""
        for _ in range(limit):
            message = read_message(sock)
            if message.type == kind:
                return message
        self.fail(f"未收到类型为 {kind} 的消息")

    def wait_for(self, predicate, timeout: float = 3.0) -> None:
        """等待后台线程完成状态变更（如踢人后的注销）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("等待条件超时")

    def assert_peer_closed(self, sock: socket.socket) -> None:
        """对端正常关闭时，把缓存的剩余消息读完后 recv 返回空字节串。"""
        sock.settimeout(3)
        while True:
            if not sock.recv(4096):
                return


class RoomLifecycleTests(HubTestBase):
    def test_create_room_binds_random_port_and_appears_in_snapshot(self) -> None:
        room = self.hub.create_room("学习室")
        self.assertGreater(room.port, 0)
        self.assertEqual(room.address, f"{self.host}:{room.port}")
        self.assertEqual(self.hub.room_count, 1)
        snapshot = self.hub.rooms_snapshot()
        self.assertEqual(snapshot[0]["name"], "学习室")
        self.assertEqual(snapshot[0]["users"], 0)

    def test_room_without_name_gets_default_name(self) -> None:
        room = self.hub.create_room()
        self.assertEqual(room.name, f"房间{room.room_id}")

    def test_duplicate_room_name_is_rejected(self) -> None:
        self.hub.create_room("学习室")
        with self.assertRaises(RuntimeError):
            self.hub.create_room("学习室")

    def test_max_rooms_limit(self) -> None:
        for index in range(self.max_rooms):
            self.hub.create_room(f"房间-{index}")
        with self.assertRaises(RuntimeError):
            self.hub.create_room("多余房间")
        self.assertEqual(self.hub.room_count, self.max_rooms)

    def test_close_room_notifies_members_and_stops_listening(self) -> None:
        room = self.hub.create_room("临时室")
        sock, _ = self.open_room(room.host, room.port)
        self.assertTrue(self.hub.close_room(room.room_id))
        notice = read_message(sock)
        self.assertEqual(notice.type, "system")
        self.assertIn("服务器关闭", notice.text)
        self.assert_peer_closed(sock)
        self.assertIsNone(self.hub.get_room(room.room_id))
        self.assertFalse(self.hub.close_room(room.room_id))

    def test_chat_inside_room_reaches_other_member(self) -> None:
        room = self.hub.create_room("聊天室")
        alice, welcome = self.open_room(room.host, room.port, "alice")
        self.assertEqual(welcome.text, "alice")
        bob, _ = self.open_room(room.host, room.port, "bob")
        send_message(alice, Message(type="chat", sender="alice", text="你好"))
        received = read_message(bob)
        self.assertEqual(received.type, "chat")
        self.assertEqual(received.text, "你好")

    def test_online_users_include_room_name(self) -> None:
        room = self.hub.create_room("统计室")
        self.open_room(room.host, room.port, "alice")
        rows = self.hub.online_users()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["room"], "统计室")
        self.assertEqual(rows[0]["name"], "alice")
        self.assertEqual(self.hub.online_count, 1)

    def test_kick_removes_member_from_room(self) -> None:
        room = self.hub.create_room("踢人室")
        alice, _ = self.open_room(room.host, room.port, "alice")
        self.assertTrue(self.hub.kick(room.room_id, "alice"))
        self.wait_for(lambda: self.hub.online_count == 0)
        self.assert_peer_closed(alice)
        self.assertFalse(self.hub.kick(room.room_id, "alice"))


class BroadcastTests(HubTestBase):
    def test_broadcast_all_reaches_every_room(self) -> None:
        first = self.hub.create_room("一号房")
        second = self.hub.create_room("二号房")
        alice, _ = self.open_room(first.host, first.port, "alice")
        bob, _ = self.open_room(second.host, second.port, "bob")
        delivered = self.hub.broadcast_all("今晚维护")
        self.assertEqual(delivered, 2)
        for sock in (alice, bob):
            message = read_message(sock)
            self.assertEqual(message.type, "system")
            self.assertEqual(message.text, "今晚维护")

    def test_empty_announcement_is_ignored(self) -> None:
        room = self.hub.create_room("静默房")
        self.open_room(room.host, room.port, "alice")
        self.assertEqual(self.hub.broadcast_all("   "), 0)


class RoomRequestTests(HubTestBase):
    def test_request_needs_approval_then_room_is_created(self) -> None:
        control, welcome = self.open_control("alice")
        self.assertEqual(welcome.type, "welcome")
        self.assertEqual(welcome.data["rooms"], [])

        send_message(control, Message(type="request_room", sender="alice", text="自习室"))
        requested = read_message(control)
        self.assertEqual(requested.type, "room_requested")
        request_id = requested.data["request_id"]

        pending = self.hub.pending_requests()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["name"], "自习室")
        self.assertEqual(self.hub.room_count, 0, "批准前不应创建房间")

        room = self.hub.approve_request(request_id)
        self.assertIsNotNone(room)
        created = self.read_until(control, "room_created")
        self.assertEqual(created.data["room"]["port"], room.port)
        self.assertEqual(self.hub.room_count, 1)

    def test_reject_request_sends_reason(self) -> None:
        control, _ = self.open_control("alice")
        send_message(control, Message(type="request_room", sender="alice", text="机房"))
        requested = read_message(control)
        request_id = requested.data["request_id"]
        self.assertTrue(self.hub.reject_request(request_id, "现在不允许开房"))
        rejected = self.read_until(control, "room_rejected")
        self.assertEqual(rejected.text, "现在不允许开房")
        self.assertEqual(self.hub.pending_requests(), [])

    def test_approve_unknown_request_returns_none(self) -> None:
        self.assertIsNone(self.hub.approve_request(999))
        self.assertFalse(self.hub.reject_request(999))


class ControlProtocolTests(HubTestBase):
    def test_list_rooms_returns_rooms(self) -> None:
        self.hub.create_room("公开房")
        control, _ = self.open_control("alice")
        send_message(control, Message(type="list_rooms", sender="alice"))
        message = read_message(control)
        self.assertEqual(message.type, "rooms")
        self.assertEqual([room["name"] for room in message.data["rooms"]], ["公开房"])

    def test_new_room_is_pushed_to_control_clients(self) -> None:
        control, _ = self.open_control("alice")
        room = self.hub.create_room("推送房")
        message = read_message(control)
        self.assertEqual(message.type, "rooms")
        self.assertEqual(message.data["rooms"][0]["id"], room.room_id)

    def test_first_message_must_be_hello(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.settimeout(5)
        self.track(sock)
        send_message(sock, Message(type="list_rooms", sender="alice"))
        message = read_message(sock)
        self.assertEqual(message.type, "error")
        self.assertIn("hello", message.text)

    def test_shutdown_closes_control_connection(self) -> None:
        control, _ = self.open_control("alice")
        self.hub.shutdown()
        self.assert_peer_closed(control)


if __name__ == "__main__":
    unittest.main()
