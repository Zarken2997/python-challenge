"""生成演示数据，方便在没有真实运行记录时查看全部功能。

数据全部由固定随机种子 ``SEED`` 生成，同一条命令多次运行得到同样的时间线；
``reset=True`` 时只清理"演示服务器"这一个实例下的数据，不动真实运行记录。

    python -m src.task2_db_system.seed            # 首次生成（已有则跳过）
    python -m src.task2_db_system.seed --reset    # 清空演示数据后重建
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import DEFAULT_DB_PATH
from .db import connect, init_db, now_text
from .recorder import classify_text

SEED = 20261127
DEMO_INSTANCE = "演示服务器"
DEMO_ROOMS = (
    ("大厅", "管理员"),
    ("答疑室", "小赵"),
    ("摸鱼角", "小钱"),
)
DEMO_USERS = ("小明", "小红", "小刚", "小美", "小赵", "小钱", "小孙", "小李")
CHAT_LINES = (
    "大家好，我是刚进来的",
    "这道题有人会吗？",
    "服务端超时怎么排查",
    "今晚的作业交了吗",
    "我把日志导进数据库了",
    "房间还能再开一个吗",
    "收到，谢谢",
)
LOG_TEMPLATES = (
    ("INFO", ("监听 127.0.0.1:{port}，等待客户端连接", "启动")),
    ("INFO", ("{user} 以昵称 {user} 加入聊天室", "加入")),
    ("INFO", ("{user} 离开聊天室", "离开")),
    ("DEBUG", ("[广播] {user}: {chat}", "聊天")),
    ("DEBUG", ("[私聊] {user} -> {other}: {chat}", "私聊")),
    ("WARNING", ("心跳超时，断开 {user}", "离开")),
    ("ERROR", ("握手失败：客户端 {user} 未按协议发送长度前缀", "错误")),
    ("INFO", ("[公告] {admin}: 服务器 22:00 维护", "公告")),
    ("INFO", ("[踢出] {user}（被管理员移出房间）", "踢出")),
)


def _seed_logs(conn: sqlite3.Connection, instance_id: int) -> int:
    """按固定节奏生成 100 余条日志，覆盖全部级别与常见事件类型。"""
    rng = random.Random(SEED)
    start = time.time() - 6 * 3600
    rows: List[tuple] = []
    for index in range(120):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start + index * 170))
        level, (template, event_type) = LOG_TEMPLATES[index % len(LOG_TEMPLATES)]
        user = DEMO_USERS[index % len(DEMO_USERS)]
        message = template.format(
            port=9000,
            user=user,
            other=DEMO_USERS[(index + 3) % len(DEMO_USERS)],
            chat=CHAT_LINES[index % len(CHAT_LINES)],
            admin="管理员",
        )
        guessed = classify_text(message)
        rows.append(
            (
                instance_id,
                ts,
                level,
                "task1.server",
                guessed["event_type"] or event_type,
                "",
                guessed["actor"],
                message,
                "",
                "seed",
            )
        )
    conn.executemany(
        "INSERT INTO log_event"
        " (instance_id, ts, level, logger, event_type, room_name, actor, message, detail, thread)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _seed_messages(
    conn: sqlite3.Connection, instance_id: int, room_ids: List[int]
) -> int:
    """生成公开聊天、私聊与系统提示三种消息，验证 ``kind`` 约束。"""
    rng = random.Random(SEED + 1)
    start = time.time() - 5 * 3600
    rows: List[tuple] = []
    for index in range(60):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start + index * 240))
        sender = DEMO_USERS[index % len(DEMO_USERS)]
        kind = "chat" if index % 5 else ("private" if index % 7 else "system")
        recipient = DEMO_USERS[(index + 1) % len(DEMO_USERS)] if kind == "private" else ""
        content = CHAT_LINES[rng.randrange(len(CHAT_LINES))]
        if kind == "system":
            content = "{0} 加入了聊天室".format(sender)
        rows.append(
            (
                instance_id,
                room_ids[index % len(room_ids)],
                ts,
                kind,
                sender,
                recipient,
                content,
                len(content),
            )
        )
    conn.executemany(
        "INSERT INTO chat_message"
        " (instance_id, room_id, ts, kind, sender, recipient, content, content_len)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _seed_operations(conn: sqlite3.Connection, instance_id: int) -> int:
    """生成管理动作审计记录，覆盖成功与失败两种结果。"""
    start = time.time() - 5 * 3600
    samples = (
        ("START", "管理员", "127.0.0.1:9000", "", "ok", "启动注册中心"),
        ("ROOM_CREATE", "管理员", "大厅", "大厅", "ok", "tcp://127.0.0.1:9001"),
        ("ROOM_CREATE", "小赵", "答疑室", "答疑室", "ok", "tcp://127.0.0.1:9002"),
        ("BROADCAST", "管理员", "全服", "", "ok", "22:00 维护通知"),
        ("KICK", "管理员", "小孙", "大厅", "ok", "重复刷屏"),
        ("KICK", "管理员", "小李", "大厅", "failed", "用户已离线"),
        ("ROOM_APPROVE", "管理员", "3", "摸鱼角", "ok", ""),
        ("ROOM_REJECT", "管理员", "4", "", "ok", "命名不合规"),
        ("ROOM_CLOSE", "管理员", "摸鱼角", "摸鱼角", "ok", "无人使用"),
        ("LOG_IMPORT", "管理员", "1", "", "ok", "导入 server-20261127.log 128 行"),
        ("LOG_PURGE", "管理员", "", "", "ok", "清理 2026-11-01 之前的日志"),
        ("STOP", "管理员", "", "", "ok", "正常关闭"),
    )
    rows = []
    for offset, (action, actor, target, room_name, result, detail) in enumerate(samples):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start + offset * 900))
        rows.append((instance_id, ts, action, actor, target, room_name, result, detail))
    conn.executemany(
        "INSERT INTO operation"
        " (instance_id, ts, action, actor, target, room_name, result, detail)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _seed_incidents(conn: sqlite3.Connection, instance_id: int) -> int:
    """生成三条事故：待处理/处理中/已关闭，演示状态流转。"""
    rows = (
        (
            "晚间高峰心跳超时",
            "高",
            "待处理",
            "管理员",
            "20:00 后连续出现 5 条心跳超时日志，需要确认是否为网络抖动。",
            "2026-11-27 20:05:00",
            "2026-11-27 20:05:00",
            None,
        ),
        (
            "日志表增长过快",
            "中",
            "处理中",
            "小赵",
            "单日写入 12 万行，准备加按天归档与清理任务。",
            "2026-11-26 09:10:00",
            "2026-11-27 10:00:00",
            None,
        ),
        (
            "数据库文件权限异常",
            "低",
            "已关闭",
            "管理员",
            "临时目录清理导致连接报错，已改为固定路径 data/chat_ops.db。",
            "2026-11-25 14:00:00",
            "2026-11-25 16:30:00",
            "2026-11-25 16:30:00",
        ),
    )
    conn.executemany(
        "INSERT INTO incident"
        " (instance_id, title, severity, status, assignee, description, created_at, updated_at,"
        "  resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(instance_id,) + row for row in rows],
    )
    return len(rows)


def seed(db_path: Optional[str | Path] = None, reset: bool = False) -> Dict[str, object]:
    """生成（或重建）演示数据，返回各表写入条数。"""
    path = init_db(db_path)
    conn = connect(path)
    try:
        existing = conn.execute(
            "SELECT id FROM server_instance WHERE name = ?", (DEMO_INSTANCE,)
        ).fetchone()
        if existing is not None and not reset:
            return {"skipped": True, "instance_id": existing["id"], "db": str(path)}
        if existing is not None:
            _purge_demo(conn, int(existing["id"]))
        start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 6 * 3600))
        with conn:
            cursor = conn.execute(
                "INSERT INTO server_instance (name, host, port, protocol, started_at, stopped_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (DEMO_INSTANCE, "127.0.0.1", 9000, "tcp", start_ts, now_text()),
            )
            instance_id = int(cursor.lastrowid)
            room_ids = []
            for offset, (name, creator) in enumerate(DEMO_ROOMS):
                created = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 6 * 3600 + offset * 60)
                )
                room_cursor = conn.execute(
                    "INSERT INTO chat_room"
                    " (instance_id, hub_room_id, name, host, port, created_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (instance_id, offset + 1, name, "127.0.0.1", 9001 + offset, creator, created),
                )
                room_ids.append(int(room_cursor.lastrowid))
            user_ids = {}
            for name in DEMO_USERS:
                existing_user = conn.execute(
                    "SELECT id FROM chat_user WHERE nickname = ?", (name,)
                ).fetchone()
                if existing_user is not None:
                    user_ids[name] = int(existing_user["id"])
                    continue
                user_cursor = conn.execute(
                    "INSERT INTO chat_user (nickname, first_seen, last_seen) VALUES (?, ?, ?)",
                    (name, start_ts, now_text()),
                )
                user_ids[name] = int(user_cursor.lastrowid)
            for offset, name in enumerate(DEMO_USERS):
                joined = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 5 * 3600 + offset * 300)
                )
                left = None if offset % 3 else now_text()
                conn.execute(
                    "INSERT INTO user_session"
                    " (instance_id, room_id, user_id, addr, joined_at, left_at, leave_reason)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        instance_id,
                        room_ids[offset % len(room_ids)],
                        user_ids[name],
                        "127.0.0.1:{0}".format(51000 + offset),
                        joined,
                        left,
                        "演示数据" if left else "",
                    ),
                )
            conn.commit()
            counts = {
                "server_instance": 1,
                "chat_room": len(room_ids),
                "chat_user": len(user_ids),
                "user_session": len(DEMO_USERS),
                "log_event": _seed_logs(conn, instance_id),
                "chat_message": _seed_messages(conn, instance_id, room_ids),
                "operation": _seed_operations(conn, instance_id),
                "incident": _seed_incidents(conn, instance_id),
            }
    finally:
        conn.close()
    result = {"skipped": False, "instance_id": instance_id, "db": str(path)}
    result.update(counts)
    return result


def _purge_demo(conn: sqlite3.Connection, instance_id: int) -> None:
    """删除演示实例的从表数据；``chat_user`` 是全局昵称表，保留不动。"""
    with conn:
        for table in ("chat_message", "log_event", "operation", "incident", "user_session"):
            conn.execute("DELETE FROM {0} WHERE instance_id = ?".format(table), (instance_id,))
        conn.execute("DELETE FROM chat_room WHERE instance_id = ?", (instance_id,))
        conn.execute("DELETE FROM server_instance WHERE id = ?", (instance_id,))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成任务2 演示数据")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="数据库路径")
    parser.add_argument("--reset", action="store_true", help="先删除演示实例的数据再重建")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = seed(args.db, reset=args.reset)
    if result.get("skipped"):
        print("演示数据已存在（实例 id={0}），加 --reset 可重建：{1}".format(
            result["instance_id"], result["db"]))
        return 0
    print("演示数据已写入 {db}".format(**result))
    for key in ("server_instance", "chat_room", "chat_user", "user_session",
                "log_event", "chat_message", "operation", "incident"):
        print("  {0:<16}{1}".format(key, result[key]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
