"""协议层单元测试：分帧、字段校验、socket 收发（使用 socketpair，不占端口）。"""

from __future__ import annotations

import json
import socket
import struct
import unittest

from src.task1_socket_chat.protocol import (
    HEADER,
    MAX_PAYLOAD,
    PROTOCOL_VERSION,
    ConnectionClosed,
    FrameDecoder,
    Message,
    ProtocolError,
    read_message,
    send_message,
)


class MessageValidationTests(unittest.TestCase):
    def test_roundtrip_keeps_fields(self) -> None:
        original = Message(type="chat", sender="alice", text="你好，世界", target=None, ts=1700000000.5)
        decoded = Message.from_dict(json.loads(original.encode()[HEADER.size :].decode("utf-8")))
        self.assertEqual(decoded.type, "chat")
        self.assertEqual(decoded.sender, "alice")
        self.assertEqual(decoded.text, "你好，世界")
        self.assertIsNone(decoded.target)
        self.assertAlmostEqual(decoded.ts, 1700000000.5, places=3)

    def test_empty_optional_fields_are_omitted(self) -> None:
        payload = Message(type="pong").to_dict()
        self.assertEqual(payload["v"], PROTOCOL_VERSION)
        self.assertNotIn("text", payload)
        self.assertNotIn("target", payload)
        self.assertNotIn("data", payload)

    def test_rejects_missing_type(self) -> None:
        with self.assertRaises(ProtocolError):
            Message.from_dict({"v": 1, "sender": "alice"})

    def test_rejects_empty_type(self) -> None:
        with self.assertRaises(ProtocolError):
            Message.from_dict({"v": 1, "type": "   "})

    def test_rejects_non_object(self) -> None:
        with self.assertRaises(ProtocolError):
            Message.from_dict(["chat"])

    def test_rejects_future_protocol_version(self) -> None:
        with self.assertRaises(ProtocolError):
            Message.from_dict({"v": PROTOCOL_VERSION + 1, "type": "chat"})

    def test_rejects_non_string_text(self) -> None:
        with self.assertRaises(ProtocolError):
            Message.from_dict({"v": 1, "type": "chat", "text": 42})

    def test_rejects_bool_timestamp(self) -> None:
        with self.assertRaises(ProtocolError):
            Message.from_dict({"v": 1, "type": "chat", "ts": True})

    def test_encode_rejects_payload_above_limit(self) -> None:
        message = Message(type="chat", text="x" * MAX_PAYLOAD)
        with self.assertRaises(ProtocolError):
            message.encode()


class FrameDecoderTests(unittest.TestCase):
    def test_decodes_single_frame(self) -> None:
        decoder = FrameDecoder()
        message = Message(type="chat", sender="alice", text="hi")
        self.assertEqual(len(decoder.feed(message.encode())), 1)

    def test_decodes_frame_split_byte_by_byte(self) -> None:
        decoder = FrameDecoder()
        frame = Message(type="chat", text="分片").encode()
        collected: list[Message] = []
        for index in range(len(frame)):
            collected.extend(decoder.feed(frame[index : index + 1]))
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0].text, "分片")
        self.assertEqual(decoder.buffered, 0)

    def test_decodes_two_frames_in_one_chunk(self) -> None:
        decoder = FrameDecoder()
        chunk = Message(type="chat", text="a").encode() + Message(type="chat", text="b").encode()
        messages = decoder.feed(chunk)
        self.assertEqual([m.text for m in messages], ["a", "b"])

    def test_keeps_partial_frame_buffered(self) -> None:
        decoder = FrameDecoder()
        frame = Message(type="chat", text="partial").encode()
        self.assertEqual(decoder.feed(frame[:-2]), [])
        self.assertGreater(decoder.buffered, 0)
        messages = decoder.feed(frame[-2:])
        self.assertEqual(messages[0].text, "partial")

    def test_rejects_zero_length_header(self) -> None:
        decoder = FrameDecoder()
        with self.assertRaises(ProtocolError):
            decoder.feed(HEADER.pack(0))

    def test_rejects_oversize_length_header(self) -> None:
        decoder = FrameDecoder()
        with self.assertRaises(ProtocolError):
            decoder.feed(HEADER.pack(MAX_PAYLOAD + 1))

    def test_rejects_invalid_json(self) -> None:
        decoder = FrameDecoder()
        body = b"{not json}"
        with self.assertRaises(ProtocolError):
            decoder.feed(HEADER.pack(len(body)) + body)


class SocketRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.left, self.right = socket.socketpair()

    def tearDown(self) -> None:
        self.left.close()
        self.right.close()

    def test_send_then_read(self) -> None:
        send_message(self.left, Message(type="chat", sender="alice", text="hello"))
        received = read_message(self.right)
        self.assertEqual((received.type, received.sender, received.text), ("chat", "alice", "hello"))

    def test_read_raises_on_eof(self) -> None:
        self.left.close()
        with self.assertRaises(ConnectionClosed):
            read_message(self.right)

    def test_read_raises_on_truncated_frame(self) -> None:
        self.left.sendall(HEADER.pack(10) + b"abc")
        self.left.close()
        with self.assertRaises(ConnectionClosed):
            read_message(self.right)

    def test_read_rejects_zero_length(self) -> None:
        self.left.sendall(struct.pack("!I", 0))
        with self.assertRaises(ProtocolError):
            read_message(self.right)
