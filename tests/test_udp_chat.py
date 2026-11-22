"""UDP 中继测试：注册、转发、用户列表、退出通知与坏数据报容错。"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest

from src.task1_socket_chat.config import UdpRelayConfig
from src.task1_socket_chat.protocol import Message
from src.task1_socket_chat.udp_chat import UdpRelay, encode_datagram


class UdpRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.relay = UdpRelay(UdpRelayConfig(host="127.0.0.1", port=0, peer_ttl=30.0))
        self.address = self.relay.start()
        self.thread = threading.Thread(target=self.relay.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.relay.shutdown()
        self.thread.join(timeout=2)

    def client(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(2.0)
        self.addCleanup(sock.close)
        return sock

    def send(self, sock: socket.socket, message: Message) -> None:
        sock.sendto(encode_datagram(message), self.address)

    def receive(self, sock: socket.socket) -> Message:
        payload, _ = sock.recvfrom(65_535)
        return Message.from_dict(json.loads(payload.decode("utf-8")))

    def read_until(self, sock: socket.socket, msg_type: str, timeout: float = 2.0) -> Message:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            message = self.receive(sock)
            if message.type == msg_type:
                return message
        raise AssertionError(f"超时未收到 {msg_type}")

    def register(self, sock: socket.socket, name: str) -> str:
        self.send(sock, Message(type="hello", sender=name))
        welcome = self.read_until(sock, "welcome")
        return welcome.text

    def test_hello_returns_welcome(self) -> None:
        alice = self.client()
        self.assertEqual(self.register(alice, "alice"), "alice")

    def test_duplicate_name_gets_suffix(self) -> None:
        first, second = self.client(), self.client()
        self.register(first, "alice")
        self.assertEqual(self.register(second, "alice"), "alice_2")

    def test_chat_is_forwarded_to_other_peers_only(self) -> None:
        alice, bob = self.client(), self.client()
        self.register(alice, "alice")
        self.register(bob, "bob")
        self.read_until(alice, "system")
        self.send(alice, Message(type="chat", sender="alice", text="广播测试"))
        received = self.read_until(bob, "chat")
        self.assertEqual((received.sender, received.text), ("alice", "广播测试"))
        alice.settimeout(0.3)
        with self.assertRaises(socket.timeout):
            self.receive(alice)

    def test_who_returns_userlist(self) -> None:
        alice, bob = self.client(), self.client()
        self.register(alice, "alice")
        self.register(bob, "bob")
        self.read_until(alice, "system")
        self.send(alice, Message(type="who", sender="alice"))
        userlist = self.read_until(alice, "userlist")
        self.assertEqual(userlist.data["users"], ["alice", "bob"])

    def test_bye_broadcasts_leave(self) -> None:
        alice, bob = self.client(), self.client()
        self.register(alice, "alice")
        self.register(bob, "bob")
        self.read_until(alice, "system")
        self.send(alice, Message(type="bye", sender="alice"))
        notice = self.read_until(bob, "system")
        self.assertIn("alice", notice.text)
        self.assertIn("离开", notice.text)

    def test_invalid_datagram_is_ignored(self) -> None:
        alice = self.client()
        alice.sendto(b"not-json", self.address)
        self.assertEqual(self.register(alice, "alice"), "alice")

    def test_chat_before_hello_returns_error(self) -> None:
        alice = self.client()
        self.send(alice, Message(type="chat", sender="alice", text="hi"))
        error = self.read_until(alice, "error")
        self.assertIn("hello", error.text)


if __name__ == "__main__":
    unittest.main()
