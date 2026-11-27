"""审计记录器：把任务1 服务器的运行日志与操作异步写进 SQLite。

为什么要一条独立写线程 + 队列：
- 聊天服务端是多线程的，网络线程绝不能被磁盘 I/O 或数据库锁拖慢；
- SQLite 单写者模型下，集中到一个线程批量提交，既减少事务次数，也避免
  ``database is locked``；
- 写库失败必须 **fail-open**：记录丢失可以接受，聊天服务中断不可接受，
  因此所有公开方法都吞掉异常并计数，绝不向调用方抛出。
"""

from __future__ import annotations

import logging
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import CONTENT_LIMIT, DEFAULT_DB_PATH, EVENT_TYPES
from .db import connect, init_db, now_text, transaction

LOGGER = logging.getLogger("task2.recorder")

# 从日志文本里猜事件类型。任务1 的日志格式已经固定，这里只做"尽力标注"；
# 结构化的操作/会话仍由 hub 回调写入，不依赖这些关键字。
EVENT_HINTS = (
    ("加入了聊天室", "加入"),
    ("离开了聊天室", "离开"),
    ("以昵称", "加入"),
    ("离开聊天室", "离开"),
    ("主动退出", "离开"),
    ("创建房间", "创建房间"),
    ("关闭房间", "关闭房间"),
    ("[踢出]", "踢出"),
    ("[公告]", "公告"),
    ("[私聊]", "私聊"),
    ("[广播]", "聊天"),
    ("监听", "启动"),
    ("服务端已启动", "启动"),
    ("服务端已停止", "停止"),
    ("握手失败", "错误"),
    ("心跳超时", "离开"),
)
_JOIN_RE = re.compile(r"以昵称 (?P<name>.+?) 加入")
_LEAVE_RE = re.compile(r"^(?P<name>.+?) 离开聊天室")
_KICK_RE = re.compile(r"\[踢出\] (?P<name>.+?)（")


def _text_time(value: Any) -> str:
    """把 ``time.time()`` 浮点数或字符串统一成库里的时间文本。"""
    if isinstance(value, (int, float)):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    text = str(value or "").strip()
    return text[:19] if len(text) >= 19 else now_text()


def classify_text(message: str, levelno: int = logging.INFO) -> Dict[str, str]:
    """按关键字把一句日志文本映射成 ``event_type/actor``（导入历史日志时复用）。"""
    event_type = ""
    for needle, mapped in EVENT_HINTS:
        if needle in message:
            event_type = mapped
            break
    if not event_type:
        event_type = "错误" if levelno >= logging.ERROR else "其他"
    actor = ""
    match = _JOIN_RE.search(message)
    if match:
        actor = match.group("name").strip()
    else:
        match = _LEAVE_RE.search(message)
        if match:
            actor = match.group("name").strip()
        else:
            match = _KICK_RE.search(message)
            if match:
                actor = match.group("name").strip()
    return {"event_type": event_type, "actor": actor}


def classify_log(record: logging.LogRecord) -> Dict[str, str]:
    """把一条 logging 记录映射成 ``event_type/actor/room_name``。

    任务1 的部分代码会给记录挂 ``event_type``/``actor``/``room_name`` 属性，
    有就直接用；没有才按文本猜。
    """
    message = record.getMessage()
    event_type = getattr(record, "event_type", "") or ""
    actor = getattr(record, "actor", "") or ""
    room_name = getattr(record, "room_name", "") or ""
    if not event_type or not actor:
        guessed = classify_text(message, record.levelno)
        event_type = event_type or guessed["event_type"]
        actor = actor or guessed["actor"]
    return {"event_type": event_type, "actor": actor, "room_name": room_name}


class QueueLogHandler(logging.Handler):
    """把 logging 记录塞进记录器队列；不格式化、不落盘、不阻塞调用线程。"""

    def __init__(self, recorder: "AuditRecorder", level: int = logging.DEBUG) -> None:
        super().__init__(level=level)
        self.recorder = recorder

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("task2"):
                # 记录器自己的日志绝不能再喂回队列，否则出错时会无限递归。
                return
            self.recorder.enqueue_log(record)
        except Exception:  # noqa: BLE001 - 日志处理器不允许抛异常
            self.handleError(record)


class AuditRecorder:
    """任务2 的写入端。一个进程一个实例，线程安全；``start()`` 之前调用也可用（先排队）。"""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        instance_name: str = "聊天服务器",
        host: str = "",
        port: int = 0,
        protocol: str = "tcp",
        flush_interval: float = 0.5,
        batch_size: int = 200,
    ) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.instance_name = instance_name
        self.host = host
        self.port = int(port)
        self.protocol = protocol
        self.flush_interval = max(0.05, float(flush_interval))
        self.batch_size = max(1, int(batch_size))
        self.instance_id: Optional[int] = None
        self.enabled = True
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._handler: Optional[QueueLogHandler] = None
        self._log_name = "task1"
        self._conn: Optional[sqlite3.Connection] = None
        self._rooms: Dict[int, int] = {}
        self._users: Dict[str, int] = {}
        self._sessions: Dict[tuple, int] = {}
        self._lock = threading.Lock()
        self.written = 0
        self.dropped = 0
        self.errors = 0

    # ------------------------------------------------------------ 生命周期

    def start(self) -> bool:
        """建库、开连接、起写线程；成功返回 True。失败只降级，不抛异常。"""
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="task2-audit-writer", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self.enabled

    def stop(self, timeout: float = 5.0) -> None:
        """停止写线程并尽量写完队列里的剩余记录。"""
        self.detach_logging()
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None
        self._conn = None
        self._rooms.clear()
        self._users.clear()
        self._sessions.clear()

    def attach_logging(self, logger_name: str = "task1", level: int = logging.DEBUG) -> None:
        """把任务1 的日志全量接入数据库。重复调用是幂等的。"""
        if self._handler is not None:
            return
        self._log_name = logger_name
        self._handler = QueueLogHandler(self, level=level)
        logging.getLogger(logger_name).addHandler(self._handler)

    def detach_logging(self) -> None:
        if self._handler is None:
            return
        logging.getLogger(self._log_name).removeHandler(self._handler)
        self._handler = None

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "db": str(self.db_path),
            "instance_id": self.instance_id,
            "pending": self._queue.qsize(),
            "written": self.written,
            "dropped": self.dropped,
            "errors": self.errors,
        }

    # ------------------------------------------------------------ 对外写入接口

    def enqueue_log(self, record: logging.LogRecord) -> None:
        """供 logging Handler 调用；只做入队，任何异常都由调用方兜住。"""
        if not self.enabled:
            return
        meta = classify_log(record)
        detail = ""
        exc_info = record.exc_info
        if exc_info:
            try:
                detail = logging.Formatter().formatException(exc_info)
            except Exception:  # noqa: BLE001 - 格式化失败就算了
                detail = ""
        self._put(
            (
                "log",
                {
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
                    "level": record.levelname,
                    "logger": record.name,
                    "event_type": meta["event_type"],
                    "room_name": meta["room_name"],
                    "actor": meta["actor"],
                    "message": record.getMessage()[:CONTENT_LIMIT],
                    "detail": detail[:CONTENT_LIMIT],
                    "thread": record.threadName or "",
                },
            )
        )

    def record_operation(
        self,
        action: str,
        actor: str = "服务器",
        target: str = "",
        room_name: str = "",
        result: str = "ok",
        detail: str = "",
    ) -> None:
        """记录一次管理操作（踢人、公告、开关房间、导入、清理……）。"""
        if not self.enabled:
            return
        self._put(
            (
                "operation",
                {
                    "ts": now_text(),
                    "action": action,
                    "actor": actor,
                    "target": target,
                    "room_name": room_name,
                    "result": result,
                    "detail": detail[:CONTENT_LIMIT],
                },
            )
        )

    def record_room_message(self, room: Any, message: Any) -> None:
        """hub 的每个房间消息回调：聊天、私聊、系统进出消息都进 chat_message。"""
        if not self.enabled:
            return
        kind = getattr(message, "type", "") or "system"
        if kind not in ("chat", "private", "system"):
            kind = "system"
        data = getattr(message, "data", None) or {}
        event = str(data.get("event", "")) if isinstance(data, dict) else ""
        self._put(
            (
                "message",
                {
                    "room": room,
                    "kind": kind,
                    "sender": getattr(message, "sender", "") or "",
                    "recipient": getattr(message, "target", "") or "",
                    "content": (getattr(message, "text", "") or "")[:CONTENT_LIMIT],
                    "event": event,
                    "ts": now_text(),
                },
            )
        )

    def record_room_created(self, room: Any) -> None:
        if self.enabled:
            self._put(("room_created", {"room": room}))

    def record_room_closed(self, room: Any, reason: str = "") -> None:
        if self.enabled:
            self._put(("room_closed", {"room": room, "reason": reason}))

    def record_server_started(self) -> None:
        self.record_operation("START", actor="服务器", detail=f"监听 {self.host}:{self.port}")

    def record_server_stopped(self) -> None:
        self.record_operation("STOP", actor="服务器", detail="服务端停止")

    # ------------------------------------------------------------ 内部：队列与落库

    def _put(self, item: tuple) -> None:
        try:
            self._queue.put_nowait(item)
        except Exception:  # noqa: BLE001 - 队列异常同样不能影响业务
            self.dropped += 1

    def _run(self) -> None:
        try:
            init_db(self.db_path)
            self._conn = connect(self.db_path)
            self._instance_id = self._ensure_instance(self._conn)
            self.instance_id = self._instance_id
        except Exception as exc:  # noqa: BLE001 - 库不可用就降级为"只聊天不记录"
            self.enabled = False
            self.errors += 1
            LOGGER.error("审计数据库不可用，已降级为不记录: %s", exc)
            self._ready.set()
            return
        self._ready.set()
        LOGGER.info("审计记录器已启动: %s (实例 #%s)", self.db_path, self._instance_id)

        while not self._stop.is_set():
            batch = self._drain()
            if batch:
                self._flush(batch)
        remaining = self._drain_all()
        if remaining:
            self._flush(remaining)
        self._close_instance()
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                LOGGER.warning("关闭审计数据库连接失败: %s", exc)
            self._conn = None

    def _drain(self) -> list:
        batch: list = []
        try:
            batch.append(self._queue.get(timeout=self.flush_interval))
        except queue.Empty:
            return batch
        while len(batch) < self.batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _drain_all(self) -> list:
        batch: list = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                return batch

    def _flush(self, batch: list) -> None:
        assert self._conn is not None
        logs = [item for kind, item in batch if kind == "log"]
        operations = [item for kind, item in batch if kind == "operation"]
        messages = [item for kind, item in batch if kind == "message"]
        rooms_created = [item for kind, item in batch if kind == "room_created"]
        rooms_closed = [item for kind, item in batch if kind == "room_closed"]
        try:
            with transaction(self._conn):
                if logs:
                    self._conn.executemany(
                        "INSERT INTO log_event (instance_id, ts, level, logger, event_type,"
                        " room_name, actor, message, detail, thread)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                self._instance_id,
                                row["ts"],
                                row["level"],
                                row["logger"],
                                row["event_type"],
                                row["room_name"],
                                row["actor"],
                                row["message"],
                                row["detail"],
                                row["thread"],
                            )
                            for row in logs
                        ],
                    )
                for row in operations:
                    self._conn.execute(
                        "INSERT INTO operation (instance_id, ts, action, actor, target,"
                        " room_name, result, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            self._instance_id,
                            row["ts"],
                            row["action"],
                            row["actor"],
                            row["target"],
                            row["room_name"],
                            row["result"],
                            row["detail"],
                        ),
                    )
                for item in rooms_created:
                    self._room_id(item["room"])
                for item in rooms_closed:
                    self._close_room(item["room"], item.get("reason", ""))
                for row in messages:
                    self._store_message(row)
            self.written += len(batch)
        except (sqlite3.Error, ValueError) as exc:
            # 一条坏数据不能拖垮整批：清空队列后已丢的计数要如实反映。
            self.errors += 1
            self.dropped += len(batch)
            LOGGER.warning("写入审计库失败，本批 %s 条已丢弃: %s", len(batch), exc)

    def _ensure_instance(self, conn: sqlite3.Connection) -> int:
        cursor = conn.execute(
            "INSERT INTO server_instance (name, host, port, protocol, started_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (self.instance_name, self.host, self.port, self.protocol, now_text()),
        )
        conn.commit()
        return int(cursor.lastrowid)

    def _close_instance(self) -> None:
        if self._conn is None or self._instance_id is None:
            return
        try:
            with transaction(self._conn):
                self._conn.execute(
                    "UPDATE server_instance SET stopped_at = ?"
                    " WHERE id = ? AND stopped_at IS NULL",
                    (now_text(), self._instance_id),
                )
        except sqlite3.Error as exc:
            LOGGER.warning("关闭运行实例失败: %s", exc)

    def _room_id(self, room: Any) -> int:
        """把任务1 的 Room 对象映射成 chat_room 主键（线程内缓存，避免每条消息查库）。"""
        assert self._conn is not None
        hub_id = int(getattr(room, "room_id", 0) or 0)
        cached = self._rooms.get(hub_id)
        if cached is not None:
            return cached
        row = self._conn.execute(
            "SELECT id FROM chat_room WHERE instance_id = ? AND hub_room_id = ?",
            (self._instance_id, hub_id),
        ).fetchone()
        if row is not None:
            self._rooms[hub_id] = int(row["id"])
            return int(row["id"])
        cursor = self._conn.execute(
            "INSERT INTO chat_room (instance_id, hub_room_id, name, host, port, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._instance_id,
                hub_id,
                getattr(room, "name", "") or f"房间{hub_id}",
                getattr(room, "host", "") or "",
                int(getattr(room, "port", 0) or 0),
                getattr(room, "created_by", "") or "",
                _text_time(getattr(room, "created_at", "")),
            ),
        )
        self._rooms[hub_id] = int(cursor.lastrowid)
        return int(cursor.lastrowid)

    def _close_room(self, room: Any, reason: str) -> None:
        assert self._conn is not None
        room_id = self._room_id(room)
        self._conn.execute(
            "UPDATE chat_room SET closed_at = ?, close_reason = ? WHERE id = ? AND closed_at IS NULL",
            (now_text(), reason, room_id),
        )
        self._rooms.pop(int(getattr(room, "room_id", 0) or 0), None)

    def _user_id(self, nickname: str) -> int:
        assert self._conn is not None
        nickname = (nickname or "").strip() or "(未知)"
        cached = self._users.get(nickname)
        if cached is not None:
            self._conn.execute(
                "UPDATE chat_user SET last_seen = ? WHERE id = ?", (now_text(), cached)
            )
            return cached
        row = self._conn.execute(
            "SELECT id FROM chat_user WHERE nickname = ?", (nickname,)
        ).fetchone()
        if row is not None:
            self._users[nickname] = int(row["id"])
            return int(row["id"])
        cursor = self._conn.execute(
            "INSERT INTO chat_user (nickname, first_seen, last_seen) VALUES (?, ?, ?)",
            (nickname, now_text(), now_text()),
        )
        self._users[nickname] = int(cursor.lastrowid)
        return int(cursor.lastrowid)

    def _store_message(self, row: Dict[str, Any]) -> None:
        assert self._conn is not None
        room_id = self._room_id(row["room"]) if row.get("room") is not None else None
        content = row["content"]
        self._conn.execute(
            "INSERT INTO chat_message (instance_id, room_id, ts, kind, sender, recipient,"
            " content, content_len) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._instance_id,
                room_id,
                row["ts"],
                row["kind"],
                row["sender"],
                row["recipient"],
                content,
                len(content),
            ),
        )
        event = row.get("event", "")
        if event in ("join", "leave") and room_id is not None:
            name = row["sender"] if row["kind"] == "chat" else ""
            if event == "join":
                text = content
                match = re.search(r"^(.+?) 加入了聊天室$", text)
                nickname = match.group(1).strip() if match else name
                if nickname:
                    user_id = self._user_id(nickname)
                    session_cursor = self._conn.execute(
                        "INSERT INTO user_session (instance_id, room_id, user_id, joined_at)"
                        " VALUES (?, ?, ?, ?)",
                        (self._instance_id, room_id, user_id, row["ts"]),
                    )
                    self._sessions[(room_id, nickname)] = int(session_cursor.lastrowid)
            else:
                match = re.search(r"^(.+?) 离开了聊天室$", content)
                nickname = match.group(1).strip() if match else name
                if nickname:
                    session_id = self._sessions.pop((room_id, nickname), None)
                    if session_id is not None:
                        self._conn.execute(
                            "UPDATE user_session SET left_at = ?, leave_reason = '正常离开'"
                            " WHERE id = ? AND left_at IS NULL",
                            (row["ts"], session_id),
                        )
                    else:
                        # 进程启动前就存在的会话（例如导入的历史），按昵称兜底关闭。
                        self._conn.execute(
                            "UPDATE user_session SET left_at = ?, leave_reason = '正常离开'"
                            " WHERE room_id = ? AND left_at IS NULL AND user_id ="
                            " (SELECT id FROM chat_user WHERE nickname = ?)",
                            (row["ts"], room_id, nickname),
                        )
        return None
