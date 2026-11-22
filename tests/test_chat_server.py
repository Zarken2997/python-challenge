"""服务器集成测试：真实回环 socket，覆盖握手、广播、私聊、心跳与异常帧。"""

from __future__ import annotations

import io
import socket
import struct
import time
import unittest
from contextlib import redirect_stdout

from src.task1_socket_chat.client import ChatClient
from src.task1_socket_chat.config import ClientConfig, ServerConfig
from src.task1_socket_chat.protocol import (
    MAX_PAYLOAD,
    ConnectionClosed,
    Message,
    read_message,
    send_message,
)
from src.task1_socket_chat.server import ChatServer


class ChatServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ChatServer(
            ServerConfig(host="127.0.0.1", port=0, ping_interval=5.0, handshake_timeout=3.0)
        )
        self.host, self.port = self.server.start()
        self.sockets: list[socket.socket] = []

    def tearDown(self) -> None:
        for sock in self.sockets:
            try:
                sock.close()
            except OSError:
                pass
        self.server.shutdown()

    def connect(self, name: str) -> tuple[socket.socket, Message]:
        sock = socket.create_connection((self.host, self.port), timeout=3.0)
        sock.settimeout(3.0)
        self.sockets.append(sock)
        send_message(sock, Message(type="hello", sender=name))
        welcome = read_message(sock)
        self.assertEqual(welcome.type, "welcome", welcome)
        return sock, welcome

    def read_until(self, sock: socket.socket, msg_type: str, timeout: float = 3.0) -> Message:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            sock.settimeout(remaining)
            message = read_message(sock)
            if message.type == msg_type:
                return message
        raise AssertionError(f"超时未收到消息类型 {msg_type}")

    def assert_quiet(self, sock: socket.socket, timeout: float = 0.3) -> None:
        sock.settimeout(timeout)
        with self.assertRaises(socket.timeout):
            read_message(sock)


class HandshakeTests(ChatServerTestCase):
    def test_welcome_reports_name_and_users(self) -> None:
        _, welcome = self.connect("alice")
        self.assertEqual(welcome.text, "alice")
        self.assertEqual(welcome.data["users"], ["alice"])

    def test_duplicate_name_gets_suffix(self) -> None:
        _, first = self.connect("alice")
        _, second = self.connect("alice")
        self.assertEqual(first.text, "alice")
        self.assertEqual(second.text, "alice_2")

    def test_first_message_must_be_hello(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=3.0)
        sock.settimeout(3.0)
        self.sockets.append(sock)
        send_message(sock, Message(type="chat", sender="alice", text="hi"))
        error = read_message(sock)
        self.assertEqual(error.type, "error")
        self.assertIn("hello", error.text)

    def test_max_clients_rejects_extra_connection(self) -> None:
        server = ChatServer(ServerConfig(host="127.0.0.1", port=0, max_clients=1))
        host, port = server.start()
        self.addCleanup(server.shutdown)
        first = socket.create_connection((host, port), timeout=3.0)
        self.addCleanup(first.close)
        first.settimeout(3.0)
        send_message(first, Message(type="hello", sender="alice"))
        self.assertEqual(read_message(first).type, "welcome")

        second = socket.create_connection((host, port), timeout=3.0)
        self.addCleanup(second.close)
        second.settimeout(3.0)
        send_message(second, Message(type="hello", sender="bob"))
        error = read_message(second)
        self.assertEqual(error.type, "error")
        self.assertIn("已满", error.text)


class BroadcastTests(ChatServerTestCase):
    def test_broadcast_reaches_other_clients_only(self) -> None:
        alice, _ = self.connect("alice")
        bob, _ = self.connect("bob")
        self.read_until(alice, "system")  # bob 的加入通知

        send_message(alice, Message(type="chat", sender="alice", text="大家好"))
        received = self.read_until(bob, "chat")
        self.assertEqual((received.sender, received.text), ("alice", "大家好"))
        self.assert_quiet(alice)

    def test_join_notice_is_broadcast(self) -> None:
        alice, _ = self.connect("alice")
        self.connect("bob")
        notice = self.read_until(alice, "system")
        self.assertEqual(notice.data["event"], "join")
        self.assertIn("bob", notice.text)

    def test_leave_notice_is_broadcast(self) -> None:
        alice, _ = self.connect("alice")
        bob, _ = self.connect("bob")
        self.read_until(alice, "system")
        send_message(bob, Message(type="bye", sender="bob"))
        notice = self.read_until(alice, "system")
        self.assertEqual(notice.data["event"], "leave")
        self.assertIn("bob", notice.text)

    def test_who_returns_sorted_userlist(self) -> None:
        alice, _ = self.connect("alice")
        self.connect("bob")
        self.read_until(alice, "system")
        send_message(alice, Message(type="who", sender="alice"))
        userlist = self.read_until(alice, "userlist")
        self.assertEqual(userlist.data["users"], ["alice", "bob"])

    def test_empty_chat_is_ignored(self) -> None:
        alice, _ = self.connect("alice")
        bob, _ = self.connect("bob")
        self.read_until(alice, "system")
        send_message(alice, Message(type="chat", sender="alice", text="   \r\n"))
        self.assert_quiet(bob)
        self.assert_quiet(alice)


class PrivateMessageTests(ChatServerTestCase):
    def test_private_reaches_only_target(self) -> None:
        alice, _ = self.connect("alice")
        bob, _ = self.connect("bob")
        carol, _ = self.connect("carol")
        self.read_until(alice, "system")
        self.read_until(alice, "system")
        self.read_until(bob, "system")  # carol 加入

        send_message(alice, Message(type="private", sender="alice", target="bob", text="悄悄话"))
        received = self.read_until(bob, "private")
        self.assertEqual((received.sender, received.text), ("alice", "悄悄话"))
        self.assert_quiet(carol)
        self.assert_quiet(alice)

    def test_private_to_unknown_user_returns_error(self) -> None:
        alice, _ = self.connect("alice")
        send_message(alice, Message(type="private", sender="alice", target="nobody", text="hi"))
        error = read_message(alice)
        self.assertEqual(error.type, "error")
        self.assertIn("nobody", error.text)


class RobustnessTests(ChatServerTestCase):
    def test_unknown_type_returns_error(self) -> None:
        alice, _ = self.connect("alice")
        send_message(alice, Message(type="dance", sender="alice"))
        error = read_message(alice)
        self.assertEqual(error.type, "error")
        self.assertIn("dance", error.text)

    def test_oversized_frame_is_rejected(self) -> None:
        alice, _ = self.connect("alice")
        alice.sendall(struct.pack("!I", MAX_PAYLOAD + 1))
        error = read_message(alice)
        self.assertEqual(error.type, "error")
        self.assertIn("协议错误", error.text)
        with self.assertRaises((ConnectionClosed, OSError)):
            read_message(alice)

    def test_server_sends_ping_when_idle_and_accepts_pong(self) -> None:
        server = ChatServer(
            ServerConfig(host="127.0.0.1", port=0, ping_interval=0.2, max_missed_pings=5)
        )
        host, port = server.start()
        self.addCleanup(server.shutdown)
        sock = socket.create_connection((host, port), timeout=3.0)
        self.addCleanup(sock.close)
        sock.settimeout(3.0)
        send_message(sock, Message(type="hello", sender="alice"))
        self.assertEqual(read_message(sock).type, "welcome")

        ping = self.read_until(sock, "ping", timeout=2.0)
        send_message(sock, Message(type="pong", sender="alice"))
        send_message(sock, Message(type="who", sender="alice"))

        seen: list[str] = []
        while True:
            message = read_message(sock)
            seen.append(message.type)
            if message.type == "userlist":
                userlist = message
                break
        self.assertNotIn("error", seen, f"心跳应答不应产生错误提示，实际收到: {seen}")
        self.assertEqual(userlist.data["users"], ["alice"])

    def test_shutdown_stops_accepting_connections(self) -> None:
        alice, _ = self.connect("alice")
        self.server.shutdown()
        with self.assertRaises(OSError):
            socket.create_connection((self.host, self.port), timeout=1.0).close()
        with self.assertRaises((ConnectionClosed, OSError)):
            for _ in range(3):  # 先排空“服务器关闭”通知，再期待 EOF/连接重置
                read_message(alice)


class ClientTests(ChatServerTestCase):
    def test_scripted_client_receives_userlist(self) -> None:
        config = ClientConfig(host=self.host, port=self.port, name="carol", linger=0.5)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = ChatClient(config).run(scripted=["/who"], linger=0.5)
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("在线用户", output)
        self.assertIn("carol", output)

    def test_client_reports_connection_failure(self) -> None:
        config = ClientConfig(host="127.0.0.1", port=1, name="carol", connect_timeout=0.2, reconnect_attempts=1)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = ChatClient(config).run(scripted=[], linger=0.0)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
