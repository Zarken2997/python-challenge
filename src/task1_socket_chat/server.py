"""TCP 聊天服务器：accept 循环 + 每个客户端一个线程。

对应题目知识点：服务端 ``socket() -> bind() -> listen(backlog) -> accept()``，
每接入一个客户端就创建一个通信线程，从而实现“一点对多点”的实时转发。
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time

from .config import MAX_TEXT_LENGTH, ServerConfig, sanitize_name
from .protocol import (
    PROTOCOL_VERSION,
    ConnectionClosed,
    Message,
    ProtocolError,
    read_message,
    send_message,
)

LOGGER = logging.getLogger("task1.server")


class ClientSession:
    """一个已接入的客户端连接及其发送锁（多线程写同一 socket 必须串行化）。"""

    __slots__ = ("sock", "addr", "name", "send_lock", "closed", "since")

    def __init__(self, sock: socket.socket, addr: tuple[str, int]) -> None:
        self.sock = sock
        self.addr = addr
        self.name = ""
        self.send_lock = threading.Lock()
        self.closed = False
        self.since = time.monotonic()

    def send(self, message: Message) -> bool:
        """发送一条消息；返回 ``False`` 表示连接已不可用。"""
        if self.closed:
            return False
        with self.send_lock:
            try:
                send_message(self.sock, message)
                return True
            except OSError as exc:
                LOGGER.debug("向 %s 发送失败: %s", self.name or self.addr, exc)
                self.closed = True
                return False


class ChatServer:
    """可启停的聊天服务器，便于在测试中直接实例化。"""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._listener: socket.socket | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._client_threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()

    @property
    def address(self) -> tuple[str, int] | None:
        """已绑定的地址；未启动时为 ``None``。"""
        if self._listener is None:
            return None
        host, port = self._listener.getsockname()[:2]
        return str(host), int(port)

    @property
    def user_count(self) -> int:
        """当前在线人数。"""
        with self._lock:
            return len(self._sessions)

    def online_users(self) -> list[dict[str, object]]:
        """返回在线客户端快照（昵称、地址、已连接秒数），供管理界面展示。

        只暴露展示用字段，不把 ``socket`` 交给界面层，避免界面误操作连接。
        """
        with self._lock:
            sessions = list(self._sessions.values())
        now = time.monotonic()
        return [
            {
                "name": session.name,
                "addr": f"{session.addr[0]}:{session.addr[1]}",
                "connected_seconds": int(now - session.since),
            }
            for session in sorted(sessions, key=lambda item: item.name)
        ]

    def broadcast_system(self, text: str) -> int:
        """向所有在线客户端发一条系统公告，返回成功送达的人数。"""
        body = text.strip()
        if not body:
            return 0
        LOGGER.info("[公告] %s", body[:80])
        message = Message(type="system", sender="server", text=body, data={"event": "notice"})
        with self._lock:
            targets = list(self._sessions.values())
        return sum(1 for session in targets if session.send(message))

    def kick(self, name: str) -> bool:
        """把指定昵称的客户端请出聊天室；返回是否找到该客户端。

        由该客户端的会话线程在读到 EOF 后完成 ``_unregister``，因此这里只关连接。
        """
        with self._lock:
            session = self._sessions.get(name)
        if session is None:
            return False
        session.send(Message(type="error", sender="server", text="你已被服务器管理员请出聊天室"))
        session.closed = True
        try:
            session.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            session.sock.close()
        except OSError:
            pass
        LOGGER.info("[踢出] %s（%s）", name, session.addr[0])
        return True

    def start(self) -> tuple[str, int]:
        """创建监听 socket 并启动 accept 线程，返回实际绑定的 ``(host, port)``。"""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.config.host, self.config.port))
            listener.listen(self.config.backlog)
        except OSError:
            listener.close()
            raise
        listener.settimeout(0.2)
        self._listener = listener
        host, port = listener.getsockname()[:2]
        self._accept_thread = threading.Thread(target=self._accept_loop, name="chat-accept", daemon=True)
        self._accept_thread.start()
        LOGGER.info("监听 %s:%s（backlog=%s）", host, port, self.config.backlog)
        return str(host), int(port)

    def serve_forever(self) -> None:
        """阻塞运行，直到 :meth:`shutdown` 被调用。"""
        if self._listener is None:
            raise RuntimeError("请先调用 start()")
        while not self._stop.wait(0.2):
            pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                conn, addr = listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    return
                LOGGER.warning("accept 失败，停止监听: %s", exc)
                return
            thread = threading.Thread(
                target=self._run_session,
                args=(conn, addr),
                name=f"chat-{addr[0]}-{addr[1]}",
                daemon=True,
            )
            with self._threads_lock:
                self._client_threads.add(thread)
            thread.start()

    def _run_session(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        session = ClientSession(conn, addr)
        try:
            self._serve_session(session)
        finally:
            session.closed = True
            with self._threads_lock:
                self._client_threads.discard(threading.current_thread())
            try:
                conn.close()
            except OSError:
                pass

    def _serve_session(self, session: ClientSession) -> None:
        conn = session.sock
        conn.settimeout(self.config.handshake_timeout)
        try:
            hello = read_message(conn)
        except socket.timeout:
            LOGGER.info("%s 握手超时", session.addr)
            return
        except (ProtocolError, ConnectionClosed, OSError) as exc:
            LOGGER.info("%s 握手失败: %s", session.addr, exc)
            return

        if hello.type != "hello":
            session.send(Message(type="error", sender="server", text="首条消息必须是 hello"))
            return

        with self._lock:
            full = len(self._sessions) >= self.config.max_clients
            if not full:
                session.name = self._unique_name(hello.sender)
                self._sessions[session.name] = session
                users = sorted(self._sessions)

        if full:
            session.send(Message(type="error", sender="server", text="服务器已满，请稍后再试"))
            return

        LOGGER.info("%s 以昵称 %s 加入", session.addr, session.name)
        session.send(
            Message(
                type="welcome",
                sender="server",
                text=session.name,
                data={"users": users, "version": PROTOCOL_VERSION},
            )
        )
        self._broadcast(
            Message(
                type="system",
                sender="server",
                text=f"{session.name} 加入了聊天室",
                data={"event": "join", "users": users},
            ),
            exclude=session.name,
        )

        try:
            self._chat_loop(session)
        finally:
            self._unregister(session)

    def _chat_loop(self, session: ClientSession) -> None:
        session.sock.settimeout(self.config.ping_interval)
        missed = 0
        while not self._stop.is_set():
            try:
                message = read_message(session.sock)
            except socket.timeout:
                missed += 1
                if missed > self.config.max_missed_pings:
                    LOGGER.info("%s 心跳超时，断开", session.name)
                    return
                if not session.send(Message(type="ping", sender="server")):
                    return
                continue
            except ProtocolError as exc:
                session.send(Message(type="error", sender="server", text=f"协议错误: {exc}"))
                return
            except (ConnectionClosed, OSError):
                return
            missed = 0
            if not self._dispatch(session, message):
                return

    def _dispatch(self, session: ClientSession, message: Message) -> bool:
        """处理一条消息；返回 ``False`` 表示应结束该会话。"""
        kind = message.type
        if kind == "chat":
            text = message.text.strip()
            if not text:
                return True
            if len(text) > MAX_TEXT_LENGTH:
                session.send(
                    Message(type="error", sender="server", text=f"消息过长（上限 {MAX_TEXT_LENGTH} 字符）")
                )
                return True
            self._broadcast(Message(type="chat", sender=session.name, text=text), exclude=session.name)
            LOGGER.info("[广播] %s: %s", session.name, text[:80])
            return True

        if kind == "private":
            target = (message.target or "").strip()
            if not target:
                session.send(Message(type="error", sender="server", text="私聊必须指定 target"))
                return True
            with self._lock:
                recipient = self._sessions.get(target)
            if recipient is None:
                session.send(Message(type="error", sender="server", text=f"用户不存在: {target}"))
                return True
            text = message.text.strip()
            if len(text) > MAX_TEXT_LENGTH:
                session.send(
                    Message(type="error", sender="server", text=f"消息过长（上限 {MAX_TEXT_LENGTH} 字符）")
                )
                return True
            recipient.send(Message(type="private", sender=session.name, text=text, target=target))
            LOGGER.info("[私聊] %s -> %s: %s", session.name, target, text[:80])
            return True

        if kind == "who":
            with self._lock:
                users = sorted(self._sessions)
            session.send(Message(type="userlist", sender="server", data={"users": users}))
            return True

        if kind == "ping":
            session.send(Message(type="pong", sender="server"))
            return True

        if kind == "pong":
            # 客户端对服务端心跳的应答：静默接受，绝不能当成未知类型回 error，
            # 否则空闲时每轮心跳都会给客户端刷一条错误提示。
            return True

        if kind == "bye":
            LOGGER.info("%s 主动退出", session.name)
            return False

        session.send(Message(type="error", sender="server", text=f"未知消息类型: {kind}"))
        return True

    def _broadcast(self, message: Message, exclude: str | None = None) -> None:
        with self._lock:
            targets = [s for name, s in self._sessions.items() if name != exclude]
        dead = [session for session in targets if not session.send(message)]
        for session in dead:
            self._unregister(session)

    def _unregister(self, session: ClientSession) -> None:
        with self._lock:
            if self._sessions.get(session.name) is not session:
                return
            del self._sessions[session.name]
            users = sorted(self._sessions)
        session.closed = True
        LOGGER.info("%s 离开聊天室", session.name)
        self._broadcast(
            Message(
                type="system",
                sender="server",
                text=f"{session.name} 离开了聊天室",
                data={"event": "leave", "users": users},
            ),
        )

    def _unique_name(self, requested: str) -> str:
        """在持锁状态下生成唯一昵称，重名时追加 ``_2``、``_3``……"""
        base = sanitize_name(requested)
        if base not in self._sessions:
            return base
        for index in range(2, 1000):
            candidate = f"{base}_{index}"
            if candidate not in self._sessions:
                return candidate
        raise ProtocolError("昵称空间已满")

    def shutdown(self) -> None:
        """停止接受新连接、通知并关闭所有客户端，等待线程退出。"""
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.send(Message(type="system", sender="server", text="服务器关闭"))
            session.closed = True
            try:
                session.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for session in sessions:
            try:
                session.sock.close()
            except OSError:
                pass
        accept_thread = self._accept_thread
        if accept_thread is not None and accept_thread is not threading.current_thread():
            accept_thread.join(timeout=2)
        with self._threads_lock:
            threads = list(self._client_threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        with self._lock:
            self._sessions.clear()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="任务1 TCP 聊天服务器（多客户端广播）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，局域网演示用 0.0.0.0")
    parser.add_argument("--port", type=int, default=9000, help="监听端口（默认 9000）")
    parser.add_argument("--backlog", type=int, default=16, help="listen 队列长度")
    parser.add_argument("--max-clients", type=int, default=50, help="最大同时在线人数")
    parser.add_argument("--ping-interval", type=float, default=30.0, help="空闲多少秒后发送心跳")
    parser.add_argument("--missed-pings", type=int, default=2, help="连续丢失多少次心跳后断开")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    config = ServerConfig(
        host=args.host,
        port=args.port,
        backlog=args.backlog,
        max_clients=args.max_clients,
        ping_interval=args.ping_interval,
        max_missed_pings=args.missed_pings,
    )
    server = ChatServer(config)
    try:
        host, port = server.start()
    except OSError as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        return 1
    print(f"聊天服务器已启动 → {host}:{port}", flush=True)
    print("按 Ctrl+C 停止服务器", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在关闭服务器…", flush=True)
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
