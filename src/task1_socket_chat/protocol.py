"""TCP 消息分帧协议：4 字节长度前缀 + UTF-8 JSON。

为什么需要分帧：TCP 是字节流，不保留消息边界。一次 ``send`` 的数据可能被拆成多次
``recv``（半包），多次 ``send`` 也可能被合并成一次 ``recv``（粘包）。因此每条消息
前面加 4 字节大端无符号整数表示正文长度，接收端先读长度、再读正文，就能还原边界。
"""

from __future__ import annotations

import json
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Final

PROTOCOL_VERSION: Final = 1
"""协议版本，双方需一致；升级不兼容改动时加 1。"""

MAX_PAYLOAD: Final = 64 * 1024
"""单帧正文上限（64 KiB），超过视为非法帧，防止恶意长度撑爆内存。"""

HEADER: Final = struct.Struct("!I")
"""帧头：网络字节序（大端）的 4 字节无符号整数。"""


class ProtocolError(ValueError):
    """收到的数据不符合协议（长度非法、JSON 损坏、字段缺失等）。"""


class ConnectionClosed(ConnectionError):
    """对端在完整消息到达前关闭了连接。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProtocolError(message)


@dataclass(slots=True)
class Message:
    """一条聊天消息。

    字段尽量少：``type`` 决定语义，``sender`` 是发送者昵称（服务器转发时改写），
    ``text`` 是正文，``target`` 仅私聊使用，``data`` 放结构化附加信息（如用户列表）。
    """

    type: str
    sender: str = ""
    text: str = ""
    target: str | None = None
    ts: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的 dict，省略空字段以减小体积。"""
        payload: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": self.type,
            "sender": self.sender,
            "ts": round(self.ts, 3),
        }
        if self.text:
            payload["text"] = self.text
        if self.target is not None:
            payload["target"] = self.target
        if self.data:
            payload["data"] = self.data
        return payload

    @classmethod
    def from_dict(cls, raw: Any) -> "Message":
        """从 JSON 解码出的对象构造消息，并做类型校验。"""
        _require(isinstance(raw, dict), "消息必须是 JSON 对象")
        assert isinstance(raw, dict)
        version = raw.get("v", PROTOCOL_VERSION)
        _require(isinstance(version, int) and version <= PROTOCOL_VERSION, f"不支持的协议版本: {version!r}")
        msg_type = raw.get("type")
        _require(isinstance(msg_type, str) and msg_type.strip() != "", "字段 type 必须是非空字符串")
        sender = raw.get("sender", "")
        _require(isinstance(sender, str), "字段 sender 必须是字符串")
        text = raw.get("text", "")
        _require(isinstance(text, str), "字段 text 必须是字符串")
        target = raw.get("target")
        _require(target is None or isinstance(target, str), "字段 target 必须是字符串或 null")
        ts = raw.get("ts", time.time())
        _require(isinstance(ts, (int, float)) and not isinstance(ts, bool), "字段 ts 必须是数字")
        data = raw.get("data", {})
        _require(isinstance(data, dict), "字段 data 必须是 JSON 对象")
        return cls(
            type=msg_type,
            sender=sender,
            text=text,
            target=target,
            ts=float(ts),
            data=data,
        )

    def encode(self) -> bytes:
        """编码为一帧：长度前缀 + JSON 正文。"""
        body = json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_PAYLOAD:
            raise ProtocolError(f"消息过大：{len(body)} 字节，上限 {MAX_PAYLOAD}")
        return HEADER.pack(len(body)) + body


def decode_frame(body: bytes) -> Message:
    """把一帧的正文（不含长度前缀）解析成 :class:`Message`。"""
    try:
        raw = json.loads(body.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"正文不是合法的 UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"正文不是合法的 JSON: {exc}") from exc
    return Message.from_dict(raw)


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
    """严格读满 ``size`` 字节；对端先关闭则返回 ``None``。"""
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            return None
        chunks += chunk
    return bytes(chunks)


def read_message(sock: socket.socket) -> Message:
    """从 socket 阻塞读取完整一条消息。

    Raises:
        ConnectionClosed: 对端在消息完整到达前关闭连接。
        ProtocolError: 帧长度非法或正文解析失败。
    """
    header = _recv_exact(sock, HEADER.size)
    if header is None:
        raise ConnectionClosed("对端已关闭连接")
    (length,) = HEADER.unpack(header)
    if length == 0 or length > MAX_PAYLOAD:
        raise ProtocolError(f"非法帧长度: {length}")
    body = _recv_exact(sock, length)
    if body is None:
        raise ConnectionClosed("帧未接收完整，对端已断开")
    return decode_frame(body)


def send_message(sock: socket.socket, message: Message) -> None:
    """编码并发送一条消息（内部使用 ``sendall``，不返回已发送字节数）。"""
    sock.sendall(message.encode())


class FrameDecoder:
    """增量分帧器：把任意切分的字节块还原成消息列表，便于测试与非阻塞 IO。"""

    def __init__(self, max_payload: int = MAX_PAYLOAD) -> None:
        self._buffer = bytearray()
        self._max_payload = max_payload

    @property
    def buffered(self) -> int:
        """当前缓冲区中尚未成帧的字节数。"""
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[Message]:
        """喂入一段字节，返回本次能解析出的所有消息（可能为空）。"""
        if chunk:
            self._buffer += chunk
        messages: list[Message] = []
        while len(self._buffer) >= HEADER.size:
            (length,) = HEADER.unpack_from(self._buffer)
            if length == 0 or length > self._max_payload:
                raise ProtocolError(f"非法帧长度: {length}")
            frame_end = HEADER.size + length
            if len(self._buffer) < frame_end:
                break
            body = bytes(self._buffer[HEADER.size:frame_end])
            del self._buffer[:frame_end]
            messages.append(decode_frame(body))
        return messages
