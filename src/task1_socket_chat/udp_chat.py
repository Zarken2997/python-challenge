"""UDP 聊天演示：无连接中继（一点对多点）。

与 TCP 版对照，本模块刻意体现 UDP 的特点：

- 没有 ``listen``/``accept``/``connect``，直接 ``sendto``/``recvfrom`` 数据报；
- 数据报自带边界，不需要长度前缀分帧；但可能丢失、重复、乱序；
- 节点用 ``hello`` 注册昵称，中继按地址记录在线列表（带 TTL 清理）。

用法::

    python -m src.task1_socket_chat.udp_chat relay --port 9100
    python -m src.task1_socket_chat.udp_chat peer  --port 9100 --name bob
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import threading
import time

if __package__ in (None, ""):
    # PyCharm 的绿色三角按「脚本」方式运行本文件时包内相对导入会失败；
    # 这里把仓库根目录加入 sys.path 并补上包名，`python -m ...` 与直接运行都可用。
    from pathlib import Path

    _REPO_ROOT = Path(__file__).resolve().parents[2]
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    __package__ = "src.task1_socket_chat"

from .client import format_incoming
from .config import MAX_TEXT_LENGTH, UdpRelayConfig, sanitize_name
from .protocol import Message, ProtocolError

LOGGER = logging.getLogger("task1.udp")

MAX_DATAGRAM = 60_000
"""单条数据报的保守上限（UDP 理论 64 KiB 减去 IP/UDP 头）。"""


def encode_datagram(message: Message) -> bytes:
    payload = json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_DATAGRAM:
        raise ProtocolError(f"数据报过大：{len(payload)} 字节")
    return payload


class Peer:
    """中继端记录的一个 UDP 节点。"""

    __slots__ = ("addr", "name", "last_seen")

    def __init__(self, addr: tuple[str, int], name: str) -> None:
        self.addr = addr
        self.name = name
        self.last_seen = time.monotonic()

    def touch(self) -> None:
        self.last_seen = time.monotonic()


class UdpRelay:
    """无连接中继：把收到的聊天数据报转发给除发送者外的所有节点。"""

    def __init__(self, config: UdpRelayConfig) -> None:
        self.config = config
        self._peers: dict[tuple[str, int], Peer] = {}
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    @property
    def address(self) -> tuple[str, int] | None:
        if self._sock is None:
            return None
        host, port = self._sock.getsockname()[:2]
        return str(host), int(port)

    def start(self) -> tuple[str, int]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.config.host, self.config.port))
        sock.settimeout(0.3)
        self._sock = sock
        host, port = sock.getsockname()[:2]
        LOGGER.info("UDP 中继监听 %s:%s", host, port)
        return str(host), int(port)

    def serve_forever(self) -> None:
        sock = self._sock
        if sock is None:
            raise RuntimeError("请先调用 start()")
        while not self._stop.is_set():
            try:
                payload, addr = sock.recvfrom(MAX_DATAGRAM + 1)
            except socket.timeout:
                self._expire_peers()
                continue
            except OSError:
                if self._stop.is_set():
                    return
                raise
            self._expire_peers()
            self._handle(payload, addr)

    def _handle(self, payload: bytes, addr: tuple[str, int]) -> None:
        if len(payload) > MAX_DATAGRAM:
            LOGGER.warning("丢弃来自 %s 的超大数据报（%s 字节）", addr, len(payload))
            return
        try:
            message = Message.from_dict(json.loads(payload.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ProtocolError) as exc:
            LOGGER.warning("丢弃来自 %s 的非法数据报: %s", addr, exc)
            return

        if message.type == "hello":
            self._register(addr, message)
            return
        peer = self._peers.get(addr)
        if peer is None:
            self._send(addr, Message(type="error", sender="relay", text="请先发送 hello 注册昵称"))
            return
        peer.touch()
        if message.type == "chat":
            text = message.text.strip()
            if not text:
                return
            if len(text) > MAX_TEXT_LENGTH:
                self._send(addr, Message(type="error", sender="relay", text="消息过长"))
                return
            for other_addr in list(self._peers):
                if other_addr != addr:
                    self._send(other_addr, Message(type="chat", sender=peer.name, text=text))
        elif message.type == "who":
            self._send(addr, Message(type="userlist", sender="relay", data={"users": self._names()}))
        elif message.type == "ping":
            self._send(addr, Message(type="pong", sender="relay"))
        elif message.type == "bye":
            self._remove(addr, announce=True)
        else:
            self._send(addr, Message(type="error", sender="relay", text=f"不支持的消息类型: {message.type}"))

    def _register(self, addr: tuple[str, int], message: Message) -> None:
        if addr not in self._peers and len(self._peers) >= self.config.max_peers:
            self._send(addr, Message(type="error", sender="relay", text="中继已满"))
            return
        name = sanitize_name(message.sender)
        used = {peer.name for peer_addr, peer in self._peers.items() if peer_addr != addr}
        base, index = name, 2
        while name in used:
            name = f"{base}_{index}"
            index += 1
        existing = self._peers.get(addr)
        if existing is not None:
            existing.name = name
            existing.touch()
        else:
            self._peers[addr] = Peer(addr, name)
            LOGGER.info("%s 以 %s 注册", addr, name)
        self._send(addr, Message(type="welcome", sender="relay", text=name, data={"users": self._names()}))
        self._broadcast(Message(type="system", sender="relay", text=f"{name} 加入（UDP 无连接）"), exclude=addr)

    def _remove(self, addr: tuple[str, int], announce: bool) -> None:
        peer = self._peers.pop(addr, None)
        if peer is None:
            return
        LOGGER.info("%s 离开", peer.name)
        if announce:
            self._broadcast(Message(type="system", sender="relay", text=f"{peer.name} 离开了"), exclude=addr)

    def _expire_peers(self) -> None:
        deadline = time.monotonic() - self.config.peer_ttl
        for addr, peer in list(self._peers.items()):
            if peer.last_seen < deadline:
                self._remove(addr, announce=True)

    def _names(self) -> list[str]:
        return sorted(peer.name for peer in self._peers.values())

    def _broadcast(self, message: Message, exclude: tuple[str, int] | None = None) -> None:
        for addr in list(self._peers):
            if addr != exclude:
                self._send(addr, message)

    def _send(self, addr: tuple[str, int], message: Message) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            sock.sendto(encode_datagram(message), addr)
        except OSError as exc:
            LOGGER.debug("向 %s 发送失败: %s", addr, exc)

    def shutdown(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()


class UdpPeer:
    """命令行 UDP 节点：注册昵称后收发群聊。"""

    def __init__(self, host: str, port: int, name: str) -> None:
        self.host = host
        self.port = port
        self.name = sanitize_name(name)
        self._sock: socket.socket | None = None
        self._closed = threading.Event()
        self._receiver: threading.Thread | None = None
        self._send_lock = threading.Lock()

    def open(self) -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", 0))
        sock.settimeout(3.0)
        self._sock = sock
        self._send(Message(type="hello", sender=self.name))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                payload, _ = sock.recvfrom(MAX_DATAGRAM)
            except socket.timeout:
                break
            try:
                message = Message.from_dict(json.loads(payload.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError, ProtocolError):
                continue
            if message.type == "welcome":
                self.name = sanitize_name(message.text or self.name)
                sock.settimeout(0.5)
                return self.name
        raise ConnectionError("UDP 中继无响应（检查端口与防火墙）")

    def start_receiver(self) -> None:
        self._receiver = threading.Thread(target=self._receive_loop, name="udp-recv", daemon=True)
        self._receiver.start()

    def _receive_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._closed.is_set():
            try:
                payload, _ = sock.recvfrom(MAX_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                message = Message.from_dict(json.loads(payload.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError, ProtocolError) as exc:
                print(f"! 收到损坏的数据报: {exc}", file=sys.stderr, flush=True)
                continue
            if message.type == "ping":
                self._send(Message(type="pong", sender=self.name))
                continue
            line = format_incoming(message)
            if line is not None:
                print(line, flush=True)

    def _send(self, message: Message) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionError("UDP socket 未打开")
        with self._send_lock:
            try:
                sock.sendto(encode_datagram(message), (self.host, self.port))
            except OSError as exc:
                raise ConnectionError(f"UDP 发送失败: {exc}") from exc

    def handle_line(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return True
        if not stripped.startswith("/"):
            self._send(Message(type="chat", sender=self.name, text=stripped[:MAX_TEXT_LENGTH]))
            print(f"[{time.strftime('%H:%M:%S')}] 我: {stripped}", flush=True)
            return True
        command, _, rest = stripped.partition(" ")
        command = command.lower()
        if command in ("/quit", "/exit"):
            return False
        if command == "/who":
            self._send(Message(type="who", sender=self.name))
            return True
        if command == "/help":
            print("UDP 演示仅支持群聊、/who、/quit（私聊请用 TCP 版）", flush=True)
            return True
        print(f"! 未知命令 {command}；UDP 演示支持 /who、/quit", file=sys.stderr)
        return True

    def quit(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._send(Message(type="bye", sender=self.name))
        except ConnectionError:
            pass
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()

    def run(self, scripted: list[str] | None = None, linger: float = 1.0) -> int:
        try:
            assigned = self.open()
        except ConnectionError as exc:
            print(f"! 连接失败: {exc}", file=sys.stderr)
            return 1
        print(f"* UDP 已注册，昵称 {assigned}（无连接，消息可能丢失）", flush=True)
        self.start_receiver()
        try:
            if scripted:
                for line in scripted:
                    if not self.handle_line(line):
                        break
                self._closed.wait(linger)
            else:
                while not self._closed.is_set():
                    try:
                        line = input("udp> ")
                    except (EOFError, KeyboardInterrupt):
                        print()
                        break
                    if not self.handle_line(line):
                        break
        finally:
            self.quit()
            if self._receiver is not None:
                self._receiver.join(timeout=2)
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="任务1 UDP 中继演示（与 TCP 版对比）")
    sub = parser.add_subparsers(dest="role", required=True)

    relay = sub.add_parser("relay", help="启动 UDP 中继")
    relay.add_argument("--host", default="127.0.0.1")
    relay.add_argument("--port", type=int, default=9100)
    relay.add_argument("--peer-ttl", type=float, default=120.0, help="节点静默多少秒后清理")

    peer = sub.add_parser("peer", help="启动 UDP 节点")
    peer.add_argument("--host", default="127.0.0.1")
    peer.add_argument("--port", type=int, default=9100)
    peer.add_argument("--name", default="guest")
    peer.add_argument("--send", action="append", default=None, help="自动发送一行，可重复")
    peer.add_argument("--linger", type=float, default=1.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    if args.role == "relay":
        relay = UdpRelay(UdpRelayConfig(host=args.host, port=args.port, peer_ttl=args.peer_ttl))
        host, port = relay.start()
        print(f"UDP 中继已启动 → {host}:{port}（Ctrl+C 退出）", flush=True)
        try:
            relay.serve_forever()
        except KeyboardInterrupt:
            print("\n收到 Ctrl+C，正在关闭…", flush=True)
        finally:
            relay.shutdown()
        return 0

    return UdpPeer(args.host, args.port, args.name).run(scripted=args.send, linger=args.linger)


if __name__ == "__main__":
    raise SystemExit(main())
