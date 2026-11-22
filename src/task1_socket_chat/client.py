"""命令行客户端：主线程读键盘输入，后台线程接收服务器消息。

对应题目知识点：客户端 ``socket() -> connect()``，之后用 ``send()``/``recv()``
双向收发；为了让“边输入边接收”互不阻塞，接收单独放在一个线程里。
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
from collections.abc import Callable

from .config import MAX_TEXT_LENGTH, ClientConfig, sanitize_name
from .protocol import ConnectionClosed, Message, ProtocolError, read_message, send_message

LOGGER = logging.getLogger("task1.client")

HELP_TEXT = """可用命令：
  /who              查看在线用户
  /to <昵称> <消息>  私聊
  /help             显示本帮助
  /quit             退出（也可按 Ctrl+C）
直接输入其它内容即群发消息。"""


def format_incoming(message: Message) -> str | None:
    """把服务器消息格式化成一行终端文本；返回 ``None`` 表示不显示（如心跳）。"""
    if message.type == "chat":
        stamp = time.strftime("%H:%M:%S", time.localtime(message.ts))
        return f"[{stamp}] {message.sender}: {message.text}"
    if message.type == "private":
        stamp = time.strftime("%H:%M:%S", time.localtime(message.ts))
        return f"[{stamp}] {message.sender} 私聊你: {message.text}"
    if message.type == "system":
        return f"* {message.text}"
    if message.type == "error":
        return f"! 服务器: {message.text}"
    if message.type == "userlist":
        users = message.data.get("users", [])
        return "在线用户: " + ("、".join(str(u) for u in users) if users else "（无）")
    if message.type == "welcome":
        users = message.data.get("users", [])
        joined = "、".join(str(u) for u in users) if users else "（无）"
        return f"* 已连接，昵称: {message.text}；在线: {joined}"
    if message.type == "pong":
        return None
    return f"? 收到未知消息类型: {message.type}"


class ChatClient:
    """一个连接的生命周期：连接 → 收发 → 退出。"""

    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.name = sanitize_name(config.name)
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._receiver: threading.Thread | None = None
        # 界面层钩子：设置 on_message 后由界面自行渲染，不再往终端打印；
        # echo=False 时本地回显也交给界面，避免同一句话出现两次。
        self.on_message: Callable[[Message], None] | None = None
        self.on_disconnect: Callable[[str], None] | None = None
        self.echo = True

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def connected(self) -> bool:
        """是否仍持有可用连接（连接成功且未被关闭）。"""
        return self._sock is not None and not self._closed.is_set()

    def connect(self) -> str:
        """建立连接并完成 hello/welcome 握手，返回服务器分配的昵称。"""
        last_error: Exception | None = None
        for attempt in range(1, self.config.reconnect_attempts + 1):
            sock: socket.socket | None = None
            try:
                sock = socket.create_connection(
                    (self.config.host, self.config.port), timeout=self.config.connect_timeout
                )
                sock.settimeout(self.config.welcome_timeout)
                self._sock = sock
                self._send(Message(type="hello", sender=self.name))
                welcome = read_message(sock)
                if welcome.type != "welcome":
                    raise ProtocolError(f"握手失败，服务器返回 {welcome.type}: {welcome.text}")
                self.name = sanitize_name(welcome.text or self.name)
                sock.settimeout(None)
                return self.name
            except ProtocolError:
                if sock is not None:
                    sock.close()
                self._sock = None
                raise
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
                self._sock = None
                if attempt < self.config.reconnect_attempts:
                    LOGGER.warning("第 %s 次连接失败（%s），%.1f 秒后重试", attempt, exc, self.config.reconnect_delay)
                    time.sleep(self.config.reconnect_delay)
        raise ConnectionError(f"连接 {self.config.host}:{self.config.port} 失败: {last_error}") from last_error

    def start_receiver(self) -> None:
        """启动后台接收线程（守护线程，退出时不阻塞进程）。"""
        self._receiver = threading.Thread(target=self._receive_loop, name="chat-recv", daemon=True)
        self._receiver.start()

    def _receive_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        reason = "连接已关闭"
        try:
            while not self._closed.is_set():
                message = read_message(sock)
                if message.type == "ping":
                    self._send(Message(type="pong", sender=self.name))
                    continue
                line = format_incoming(message)
                if line is None:
                    continue
                if self.on_message is not None:
                    self.on_message(message)
                else:
                    print(line, flush=True)
        except (ConnectionClosed, OSError) as exc:
            if not self._closed.is_set():
                reason = f"与服务器的连接已断开: {exc}"
                self._report_disconnect(reason)
        except ProtocolError as exc:
            reason = f"协议错误: {exc}"
            self._report_disconnect(reason)
        finally:
            self._closed.set()
            if self.on_disconnect is not None:
                self.on_disconnect(reason)

    def _report_disconnect(self, text: str) -> None:
        """没有界面钩子时按终端习惯把断线原因写到 stderr。"""
        if self.on_disconnect is None:
            print(f"\n! {text}", file=sys.stderr, flush=True)

    def _send(self, message: Message) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionError("尚未连接服务器")
        with self._send_lock:
            try:
                send_message(sock, message)
            except OSError as exc:
                self._closed.set()
                raise ConnectionError(f"发送失败: {exc}") from exc

    def send_chat(self, text: str) -> None:
        """群发一条消息（本地先回显，服务器不会再回传给发送者）。"""
        body = text.strip()
        if not body:
            return
        if len(body) > MAX_TEXT_LENGTH:
            print(f"! 消息过长（上限 {MAX_TEXT_LENGTH} 字符）", file=sys.stderr)
            return
        self._send(Message(type="chat", sender=self.name, text=body))
        stamp = time.strftime("%H:%M:%S")
        if self.echo:
            print(f"[{stamp}] {self.name}: {body}", flush=True)

    def send_private(self, target: str, text: str) -> None:
        body = text.strip()
        if not body:
            print("! 私聊内容不能为空", file=sys.stderr)
            return
        if len(body) > MAX_TEXT_LENGTH:
            print(f"! 消息过长（上限 {MAX_TEXT_LENGTH} 字符）", file=sys.stderr)
            return
        self._send(Message(type="private", sender=self.name, target=target, text=body))
        stamp = time.strftime("%H:%M:%S")
        if self.echo:
            print(f"[{stamp}] 我 → {target}: {body}", flush=True)

    def request_users(self) -> None:
        """向服务器请求在线用户列表（界面上的“刷新在线列表”按钮）。"""
        self._send(Message(type="who", sender=self.name))

    def handle_line(self, line: str) -> bool:
        """处理一行输入；返回 ``False`` 表示应退出。"""
        stripped = line.strip()
        if not stripped:
            return True
        if not stripped.startswith("/"):
            self.send_chat(stripped)
            return True
        command, _, rest = stripped.partition(" ")
        command = command.lower()
        rest = rest.strip()
        if command in ("/quit", "/exit"):
            return False
        if command == "/help":
            print(HELP_TEXT, flush=True)
            return True
        if command == "/who":
            self._send(Message(type="who", sender=self.name))
            return True
        if command == "/to":
            target, _, text = rest.partition(" ")
            if not target or not text.strip():
                print("用法: /to <昵称> <消息>", file=sys.stderr)
                return True
            self.send_private(target, text)
            return True
        print(f"! 未知命令 {command}，输入 /help 查看帮助", file=sys.stderr)
        return True

    def quit(self) -> None:
        """尽力发送 bye，然后关闭 socket 并唤醒接收线程。"""
        if self._closed.is_set():
            return
        self._closed.set()
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            with self._send_lock:
                send_message(sock, Message(type="bye", sender=self.name))
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def run(self, scripted: list[str] | None = None, linger: float = 1.0) -> int:
        """运行客户端；``scripted`` 非空时按行自动发送（用于演示与回归）。"""
        try:
            assigned = self.connect()
        except (ConnectionError, ProtocolError) as exc:
            print(f"! 连接失败: {exc}", file=sys.stderr)
            return 1
        print(f"* 已连接 {self.config.host}:{self.config.port}，昵称 {assigned}", flush=True)
        self.start_receiver()
        try:
            if scripted:
                for line in scripted:
                    if self.closed or not self.handle_line(line):
                        break
                self._closed.wait(linger)
            else:
                print(HELP_TEXT, flush=True)
                while not self.closed:
                    try:
                        line = input("> ")
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
    parser = argparse.ArgumentParser(description="任务1 TCP 聊天客户端")
    parser.add_argument("--host", default="127.0.0.1", help="服务器地址")
    parser.add_argument("--port", type=int, default=9000, help="服务器端口")
    parser.add_argument("--name", default="guest", help="昵称，重名时服务器会自动加后缀")
    parser.add_argument("--connect-timeout", type=float, default=5.0, help="连接超时（秒）")
    parser.add_argument("--send", action="append", default=None, help="自动发送一行，可重复（非交互演示）")
    parser.add_argument("--linger", type=float, default=1.0, help="自动发送后等待回包的时间（秒）")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    config = ClientConfig(
        host=args.host,
        port=args.port,
        name=args.name,
        connect_timeout=args.connect_timeout,
        linger=args.linger,
    )
    return ChatClient(config).run(scripted=args.send, linger=args.linger)


if __name__ == "__main__":
    raise SystemExit(main())
