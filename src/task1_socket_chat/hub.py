"""聊天室注册中心（Hub）：一个控制端口统一管理多个聊天室。

设计动机（对照题目要求）：

- "一点对多点"不只指一个房间里多人聊天，也允许**多个聊天室同时存在**；
- 每个聊天室是独立的 :class:`~.server.ChatServer`，监听各自的端口，
  客户端"通过相同的服务器地址 + 端口"进入同一个房间（题目原话）；
- 控制端口（默认 9000）只做三件事：返回房间列表、接收开房申请、把审批结果回给申请人；
- 开房申请必须由服务器端批准：客户端发 ``request_room``，服务器监控台点「批准」后才真正监听新端口；
- 服务器端自己也可以直接创建/关闭聊天室，并可以向所有房间发全服公告。

线程模型：控制连接每个客户端一个线程（与聊天室一致），房间列表用一把锁保护；
所有公开方法都可从 GUI 主线程安全调用。
"""

from __future__ import annotations

import argparse
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

from .config import DEFAULT_HOST, HubConfig, ServerConfig, sanitize_name
from .protocol import (
    PROTOCOL_VERSION,
    ConnectionClosed,
    Message,
    ProtocolError,
    read_message,
    send_message,
)
from .server import ChatServer, ClientSession

LOGGER = logging.getLogger("task1.hub")


class Room:
    """一个正在监听的聊天室：名称、地址、底层服务器与创建者。"""

    __slots__ = ("room_id", "name", "server", "host", "port", "created_by", "created_at")

    def __init__(
        self,
        room_id: int,
        name: str,
        server: ChatServer,
        host: str,
        port: int,
        created_by: str,
    ) -> None:
        self.room_id = room_id
        self.name = name
        self.server = server
        self.host = host
        self.port = port
        self.created_by = created_by
        self.created_at = time.time()

    @property
    def address(self) -> str:
        """``host:port`` 形式的连接地址，客户端直接粘贴即可。"""
        return f"{self.host}:{self.port}"

    def snapshot(self) -> dict[str, object]:
        """给界面与客户端看的房间信息（不含 server 对象）。"""
        return {
            "id": self.room_id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "address": self.address,
            "users": self.server.user_count,
            "created_by": self.created_by,
        }


class RoomRequest:
    """客户端提交、等待服务器批准的开房申请。"""

    __slots__ = ("request_id", "name", "requester", "created_at", "reply")

    def __init__(self, request_id: int, name: str, requester: str, reply) -> None:
        self.request_id = request_id
        self.name = name
        self.requester = requester
        self.created_at = time.time()
        self.reply = reply

    def snapshot(self) -> dict[str, object]:
        return {
            "id": self.request_id,
            "name": self.name,
            "requester": self.requester,
            "created_at": self.created_at,
        }


class ChatHub:
    """管理多个聊天室的控制中心。"""

    def __init__(self, config: HubConfig | None = None) -> None:
        self.config = config or HubConfig()
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._rooms: dict[int, Room] = {}
        self._requests: dict[int, RoomRequest] = {}
        self._control_clients: dict[str, ClientSession] = {}
        self._next_room_id = 1
        self._next_request_id = 1
        self._accept_thread: threading.Thread | None = None
        self._threads: set[threading.Thread] = set()

    # ------------------------------------------------------------ 生命周期

    @property
    def address(self) -> tuple[str, int] | None:
        """控制端口地址；未启动时为 ``None``。"""
        if self._listener is None:
            return None
        host, port = self._listener.getsockname()[:2]
        return str(host), int(port)

    def start(self) -> tuple[str, int]:
        """监听控制端口并开始接受控制连接，返回实际地址。"""
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
        self._accept_thread = threading.Thread(target=self._accept_loop, name="hub-accept", daemon=True)
        self._accept_thread.start()
        LOGGER.info("控制端口已启动 → %s:%s", host, port)
        return str(host), int(port)

    def serve_forever(self) -> None:
        if self._listener is None:
            raise RuntimeError("请先调用 start()")
        while not self._stop.wait(0.2):
            pass

    def shutdown(self) -> None:
        """关闭所有聊天室，再停止控制端口。"""
        self._stop.set()
        for room in self._rooms_snapshot():
            self.close_room(room.room_id, announce="服务器关闭")
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            clients = list(self._control_clients.values())
        for session in clients:
            session.send(Message(type="system", sender="server", text="服务器关闭"))
            session.closed = True
            try:
                session.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for session in clients:
            try:
                session.sock.close()
            except OSError:
                pass
        if self._accept_thread is not None and self._accept_thread is not threading.current_thread():
            self._accept_thread.join(timeout=2)
        with self._lock:
            self._control_clients.clear()
        LOGGER.info("注册中心已停止")

    # ------------------------------------------------------------ 房间管理

    def _rooms_snapshot(self) -> list[Room]:
        with self._lock:
            return [self._rooms[key] for key in sorted(self._rooms)]

    def rooms_snapshot(self) -> list[dict[str, object]]:
        """所有聊天室的展示用快照，按创建顺序。"""
        return [room.snapshot() for room in self._rooms_snapshot()]

    @property
    def room_count(self) -> int:
        with self._lock:
            return len(self._rooms)

    @property
    def online_count(self) -> int:
        return sum(room.server.user_count for room in self._rooms_snapshot())

    def online_users(self) -> list[dict[str, object]]:
        """所有聊天室的在线用户合并列表，附带房间名，便于监控台汇总。"""
        rows: list[dict[str, object]] = []
        for room in self._rooms_snapshot():
            for user in room.server.online_users():
                rows.append(
                    {
                        "room": room.name,
                        "room_id": room.room_id,
                        "name": user["name"],
                        "addr": user["addr"],
                        "connected_seconds": user["connected_seconds"],
                    }
                )
        return sorted(rows, key=lambda row: (str(row["room"]), str(row["name"])))

    def get_room(self, room_id: int) -> Room | None:
        with self._lock:
            return self._rooms.get(room_id)

    def create_room(self, name: str = "", created_by: str = "服务器") -> Room:
        """新建聊天室并开始监听；端口由系统自动分配。

        端口传 0（默认）是刻意的：课堂上常见 ``9000 已被占用`` 的尴尬，
        让系统挑空闲端口最省事，真实端口会显示在监控台与申请回复里。
        """
        with self._lock:
            if len(self._rooms) >= self.config.max_rooms:
                raise RuntimeError(f"聊天室数量已达上限 {self.config.max_rooms}")
            room_id = self._next_room_id
            room_name = (name or "").strip() or f"房间{room_id}"
            existing = {room.name for room in self._rooms.values()}
            if room_name in existing:
                raise RuntimeError(f"聊天室名称已存在: {room_name}")
            self._next_room_id += 1
        server = ChatServer(ServerConfig(host=self.config.host, port=0))
        try:
            host, port = server.start()
        except OSError as exc:
            raise RuntimeError(f"聊天室监听失败: {exc}") from exc
        room = Room(room_id, room_name, server, host, port, sanitize_name(created_by))
        with self._lock:
            self._rooms[room_id] = room
        LOGGER.info("[聊天室] 创建「%s」→ %s（申请人：%s）", room.name, room.address, room.created_by)
        self._push_rooms()
        return room

    def close_room(self, room_id: int, announce: str = "本聊天室已被服务器关闭") -> bool:
        """关闭聊天室：先通知房内所有人，再停止监听。"""
        with self._lock:
            room = self._rooms.pop(room_id, None)
        if room is None:
            return False
        room.server.broadcast_system(announce)
        room.server.shutdown()
        LOGGER.info("[聊天室] 关闭「%s」（%s）", room.name, room.address)
        self._push_rooms()
        return True

    def broadcast_all(self, text: str) -> int:
        """向所有聊天室发公告，返回送达人数。"""
        body = text.strip()
        if not body:
            return 0
        LOGGER.info("[全服公告] %s", body[:80])
        total = 0
        for room in self._rooms_snapshot():
            total += room.server.broadcast_system(body)
        return total

    def kick(self, room_id: int, name: str) -> bool:
        """把某个房间里的指定昵称踢出。"""
        room = self.get_room(room_id)
        if room is None:
            return False
        return room.server.kick(name)

    # ------------------------------------------------------------ 开房申请

    def submit_request(self, name: str, requester: str, reply) -> RoomRequest:
        """记录一条开房申请；``reply`` 是给申请人回消息的回调。"""
        with self._lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            request = RoomRequest(request_id, (name or "").strip(), sanitize_name(requester), reply)
            self._requests[request_id] = request
        LOGGER.info("[申请] %s 申请创建聊天室「%s」", request.requester, request.name or "（自动命名）")
        return request

    def pending_requests(self) -> list[dict[str, object]]:
        with self._lock:
            return [self._requests[key].snapshot() for key in sorted(self._requests)]

    def approve_request(self, request_id: int) -> Room | None:
        """批准申请：创建聊天室并把地址回给申请人。"""
        with self._lock:
            request = self._requests.pop(request_id, None)
        if request is None:
            return None
        try:
            room = self.create_room(request.name, created_by=request.requester)
        except RuntimeError as exc:
            request.reply(Message(type="room_rejected", sender="server", text=str(exc)))
            LOGGER.warning("[申请] 批准失败：%s", exc)
            return None
        request.reply(
            Message(
                type="room_created",
                sender="server",
                text=room.name,
                data={"room": room.snapshot()},
            )
        )
        LOGGER.info("[申请] 已批准 %s 的申请 → %s", request.requester, room.address)
        return room

    def reject_request(self, request_id: int, reason: str = "服务器未批准该申请") -> bool:
        with self._lock:
            request = self._requests.pop(request_id, None)
        if request is None:
            return False
        request.reply(Message(type="room_rejected", sender="server", text=reason))
        LOGGER.info("[申请] 已拒绝 %s 的申请（%s）", request.requester, reason)
        return True

    # ------------------------------------------------------------ 控制连接

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
                LOGGER.warning("控制端口 accept 失败: %s", exc)
                return
            thread = threading.Thread(
                target=self._run_control,
                args=(conn, addr),
                name=f"hub-{addr[0]}-{addr[1]}",
                daemon=True,
            )
            with self._lock:
                self._threads.add(thread)
            thread.start()

    def _run_control(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        session = ClientSession(conn, addr)
        try:
            self._serve_control(session)
        finally:
            session.closed = True
            with self._lock:
                self._threads.discard(threading.current_thread())
                if self._control_clients.get(session.name) is session:
                    del self._control_clients[session.name]
            try:
                conn.close()
            except OSError:
                pass

    def _serve_control(self, session: ClientSession) -> None:
        conn = session.sock
        conn.settimeout(self.config.handshake_timeout)
        try:
            hello = read_message(conn)
        except (socket.timeout, ProtocolError, ConnectionClosed, OSError):
            return
        if hello.type != "hello":
            session.send(Message(type="error", sender="server", text="首条消息必须是 hello"))
            return
        with self._lock:
            base = sanitize_name(hello.sender)
            name = base
            index = 1
            while name in self._control_clients:
                index += 1
                name = f"{base}_{index}"
            session.name = name
            self._control_clients[name] = session
        session.send(
            Message(
                type="welcome",
                sender="server",
                text=session.name,
                data={"rooms": self.rooms_snapshot(), "version": PROTOCOL_VERSION},
            )
        )
        LOGGER.info("控制连接 %s 以昵称 %s 接入", session.addr, session.name)
        conn.settimeout(self.config.ping_interval)
        missed = 0
        while not self._stop.is_set():
            try:
                message = read_message(conn)
            except socket.timeout:
                missed += 1
                if missed > self.config.max_missed_pings:
                    return
                if not session.send(Message(type="ping", sender="server")):
                    return
                continue
            except (ProtocolError, ConnectionClosed, OSError):
                return
            missed = 0
            if not self._dispatch(session, message):
                return

    def _dispatch(self, session: ClientSession, message: Message) -> bool:
        kind = message.type
        if kind == "list_rooms":
            session.send(Message(type="rooms", sender="server", data={"rooms": self.rooms_snapshot()}))
            return True

        if kind == "request_room":
            request = self.submit_request(message.text, session.name, session.send)
            session.send(
                Message(
                    type="room_requested",
                    sender="server",
                    text=request.name,
                    data={"request_id": request.request_id, "name": request.name},
                )
            )
            self._push_rooms()
            return True

        if kind == "ping":
            session.send(Message(type="pong", sender="server"))
            return True

        if kind == "pong":
            return True

        if kind == "bye":
            return False

        session.send(Message(type="error", sender="server", text=f"控制端口不支持的消息类型: {kind}"))
        return True

    def _push_rooms(self) -> None:
        """房间增删后主动推一次列表，客户端不必等下一次轮询。"""
        with self._lock:
            clients = list(self._control_clients.values())
        if not clients:
            return
        message = Message(type="rooms", sender="server", data={"rooms": self.rooms_snapshot()})
        for session in clients:
            session.send(message)


class HubClient:
    """客户端侧的控制连接：拿房间列表、提交开房申请、接收审批结果。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = 9000, name: str = "guest") -> None:
        self.host = host
        self.port = port
        self.name = sanitize_name(name)
        self.nickname = self.name
        self.rooms: list[dict[str, object]] = []
        self.on_message = None  # type: ignore[assignment]
        self.on_disconnect = None  # type: ignore[assignment]
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closed

    def connect(self, timeout: float = 5.0) -> None:
        """连接控制端口并完成 hello/welcome 握手；失败抛 ``OSError``。"""
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.settimeout(timeout)
        try:
            send_message(sock, Message(type="hello", sender=self.name))
            welcome = read_message(sock)
        except (ProtocolError, ConnectionClosed, OSError):
            sock.close()
            raise
        if welcome.type == "welcome":
            self.nickname = welcome.text or self.name
            self.rooms = list(welcome.data.get("rooms") or [])
        sock.settimeout(None)
        self._sock = sock
        self._closed = False

    def start_receiver(self) -> threading.Thread:
        thread = threading.Thread(target=self._receive_loop, name="hub-client", daemon=True)
        thread.start()
        return thread

    def _receive_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            while True:
                message = read_message(sock)
                if message.type == "ping":
                    self.send(Message(type="pong", sender=self.nickname))
                    continue
                if message.type == "rooms":
                    self.rooms = list(message.data.get("rooms") or [])
                elif message.type == "welcome":
                    self.nickname = message.text or self.nickname
                if self.on_message is not None:
                    self.on_message(message)
        except (ConnectionClosed, ProtocolError, OSError) as exc:
            self._closed = True
            if self.on_disconnect is not None:
                self.on_disconnect(str(exc))

    def send(self, message: Message) -> bool:
        with self._lock:
            sock = self._sock
            if sock is None or self._closed:
                return False
            try:
                send_message(sock, message)
                return True
            except OSError:
                self._closed = True
                return False

    def request_rooms(self) -> bool:
        return self.send(Message(type="list_rooms", sender=self.nickname))

    def request_room(self, name: str) -> bool:
        return self.send(Message(type="request_room", sender=self.nickname, text=name))

    def close(self) -> None:
        self._closed = True
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            send_message(sock, Message(type="bye", sender=self.nickname))
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="任务1 聊天室注册中心（多聊天室 + 开房审批）")
    parser.add_argument("--host", default=DEFAULT_HOST, help="控制端口监听地址")
    parser.add_argument("--port", type=int, default=9000, help="控制端口，客户端启动后连这里")
    parser.add_argument("--max-rooms", type=int, default=20, help="聊天室数量上限")
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
    hub = ChatHub(HubConfig(host=args.host, port=args.port, max_rooms=args.max_rooms))
    try:
        host, port = hub.start()
    except OSError as exc:
        print(f"控制端口启动失败: {exc}", file=sys.stderr)
        return 1
    print(f"注册中心已启动 → {host}:{port}（客户端连这个端口申请/查看聊天室）", flush=True)
    try:
        hub.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在关闭…", flush=True)
    finally:
        hub.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
