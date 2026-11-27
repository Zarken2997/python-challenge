"""任务2 命令行界面：聊天服务器运维日志管理系统的字符界面。

菜单覆盖课程要求的六类操作：浏览、排序、查询、增、删、改，
另外提供统计概览、历史日志导入、CSV 导出和过期日志清理。
所有输入都经过校验，非法输入只会重新提问，不会退出程序。

    python -m src.task2_db_system.cli            # 进入菜单
    python -m src.task2_db_system.cli --demo     # 自动跑一遍只读演示（报告截图用）
    python -m src.task2_db_system.cli --stats    # 只打印统计概览
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .config import (
    ACTION_LABELS,
    DEFAULT_DB_PATH,
    EVENT_TYPES,
    LOG_LEVELS,
    MAX_EXPORT_ROWS,
    MESSAGE_KINDS,
    PAGE_SIZE,
    SEVERITIES,
    STATUSES,
)
from .db import connect, init_db
from .importer import import_log_file
from .repository import Repository
from .stats import (
    action_counts,
    event_type_counts,
    hourly_counts,
    incident_summary,
    level_counts,
    overview,
)

LOGGER = logging.getLogger("task2.cli")

MENU = """
========== 聊天服务器运维日志管理系统 ==========
 1. 运行日志    浏览 / 筛选 / 排序 / 分页
 2. 聊天记录    按房间、发送者、类型查询
 3. 操作审计    管理员动作记录
 4. 事故记录    增 / 删 / 改 / 查
 5. 统计概览    各表数量、错误趋势、活跃房间
 6. 基础数据    实例 / 房间 / 用户 / 会话
 7. 导入日志    把任务1 的 .log 文本导入数据库
 8. 导出 CSV    把当前筛选结果导出
 9. 清理日志    删除指定时间之前的日志
 0. 退出
===============================================
"""


# ------------------------------------------------------------------ 输出格式


def _width(text: object) -> int:
    """按终端显示宽度计算：中文/全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _clip(text: object, limit: int) -> str:
    """把单元格裁到 ``limit`` 个显示列，超出用 ``…`` 结尾。"""
    raw = str(text if text is not None else "").replace("\n", " ")
    if _width(raw) <= limit:
        return raw
    kept = []
    used = 0
    for ch in raw:
        step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + step > limit - 1:
            break
        kept.append(ch)
        used += step
    return "".join(kept) + "…"


def _pad(text: object, width: int) -> str:
    return str(text) + " " * max(0, width - _width(text))


def format_table(
    headers: Sequence[str], rows: Sequence[Sequence[object]], max_col: int = 30
) -> List[str]:
    """生成等宽表格（列表形式返回，便于逐行 print 或写入文件）。"""
    cells = [[_clip(cell, max_col) for cell in row] for row in rows]
    widths = []
    for index, header in enumerate(headers):
        body = max([_width(row[index]) for row in cells] + [1])
        widths.append(max(_width(header), body))
    lines = [" | ".join(_pad(h, widths[i]) for i, h in enumerate(headers))]
    lines.append("-+-".join("-" * width for width in widths))
    for row in cells:
        lines.append(" | ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)))
    return lines


def print_table(
    headers: Sequence[str], rows: Sequence[Sequence[object]], max_col: int = 30
) -> None:
    for line in format_table(headers, rows, max_col):
        print(line)
    if not rows:
        print("（没有符合条件的记录）")


# ------------------------------------------------------------------ 输入校验


def ask(prompt: str, default: str = "") -> str:
    """读一行输入；EOF（管道/重定向）时返回空串，让菜单自行退出。"""
    suffix = " [{0}]".format(default) if default else ""
    try:
        value = input("{0}{1}: ".format(prompt, suffix)).strip()
    except EOFError:
        return ""
    return value or default


def ask_int(prompt: str, default: Optional[int] = None) -> Optional[int]:
    """读整数；直接回车返回默认值，非法输入重新提问。"""
    while True:
        text = ask(prompt, "" if default is None else str(default))
        if text == "":
            return default
        try:
            return int(text)
        except ValueError:
            print("  请输入数字。")


def ask_choice(prompt: str, options: Sequence[str], allow_empty: bool = True) -> str:
    """从给定选项里选一个，允许回车跳过。"""
    shown = "/".join(str(option) for option in options)
    while True:
        text = ask("{0}（{1}）".format(prompt, shown))
        if not text and allow_empty:
            return ""
        if text in options:
            return text
        print("  只能填：{0}".format(shown))


def ask_index(prompt: str, total: int, default: int = 1) -> int:
    while True:
        value = ask_int(prompt, default)
        if value is None:
            return default
        if 1 <= value <= max(total, 1):
            return value
        print("  页码需在 1 ~ {0} 之间。".format(max(total, 1)))


# ------------------------------------------------------------------ 分页浏览


def _page_loop(
    title: str,
    fetch: Callable[[int, int], Tuple[List[Tuple[str, ...]], int]],
) -> None:
    """通用分页器：``fetch(page, size)`` 返回（表格行, 总页数）。"""
    page, size = 1, PAGE_SIZE
    while True:
        rows, pages = fetch(page, size)
        print("\n--- {0} 第 {1}/{2} 页（每页 {3} 条）---".format(title, page, max(pages, 1), size))
        print_table(*_split_rows(title, rows))
        action = ask("n 下一页 / p 上一页 / 页码 / s 每页条数 / q 返回", "q").lower()
        if action in ("q", ""):
            return
        if action == "n":
            page = min(page + 1, max(pages, 1))
        elif action == "p":
            page = max(page - 1, 1)
        elif action == "s":
            size = ask_int("每页条数", size) or size
            size = max(5, min(size, 500))
            page = 1
        elif action.isdigit():
            page = ask_index("跳到第几页", pages, int(action))


_TABLE_HEADERS: Dict[str, Tuple[str, ...]] = {
    "运行日志": ("时间", "级别", "类型", "人员", "内容"),
    "聊天记录": ("时间", "房间", "类型", "发送者", "内容"),
    "操作审计": ("时间", "动作", "操作者", "对象", "结果", "备注"),
    "事故记录": ("ID", "标题", "级别", "状态", "负责人", "更新时间"),
    "实例": ("ID", "名称", "地址", "协议", "启动时间", "停止时间"),
    "房间": ("ID", "房间", "地址", "创建者", "创建时间", "关闭时间"),
    "用户": ("ID", "昵称", "首次出现", "最近出现", "备注"),
    "会话": ("房间", "昵称", "地址", "进入", "离开", "原因"),
}


def _split_rows(title: str, rows: Sequence[Sequence[object]]) -> Tuple[Tuple[str, ...], list]:
    return _TABLE_HEADERS.get(title, ()), rows


# ------------------------------------------------------------------ 日志


def browse_logs(repo: Repository) -> None:
    keyword = ask("关键字（内容/人员/房间，可空）")
    level = ask_choice("级别", LOG_LEVELS)
    event_type = ask_choice("事件类型", EVENT_TYPES)
    room = ask("房间名（可空）")
    actor = ask("人员（可空）")
    start = ask("起始时间 YYYY-MM-DD（可空）")
    end = ask("结束时间 YYYY-MM-DD（可空）")
    sort = ask_choice("排序字段 ts/level/event_type/actor", ("ts", "level", "event_type", "actor"))
    order = ask("倒序？y/n", "y")
    desc = order.lower() != "n"

    def fetch(page: int, size: int):
        result = repo.list_logs(
            keyword=keyword, level=level, event_type=event_type, room=room, actor=actor,
            start=start, end=end, sort=sort or "ts", desc=desc, page=page, page_size=size,
        )
        rows = [(item["ts"], item["level"], item["event_type"], item["actor"], item["message"])
                for item in result["rows"]]
        return rows, result["pages"]

    _page_loop("运行日志", fetch)


def browse_messages(repo: Repository) -> None:
    keyword = ask("关键字（内容）")
    kind = ask_choice("类型", MESSAGE_KINDS)
    room = ask("房间名（可空）")
    sender = ask("发送者（可空）")
    start = ask("起始时间 YYYY-MM-DD（可空）")
    end = ask("结束时间 YYYY-MM-DD（可空）")

    def fetch(page: int, size: int):
        result = repo.list_messages(
            keyword=keyword, kind=kind, room=room, sender=sender, start=start, end=end,
            page=page, page_size=size,
        )
        rows = [(item["ts"], item["room_name"] or "", item["kind"], item["sender"], item["content"])
                for item in result["rows"]]
        return rows, result["pages"]

    _page_loop("聊天记录", fetch)


def browse_operations(repo: Repository) -> None:
    action = ask_choice("动作", tuple(ACTION_LABELS.keys()))
    actor = ask("操作者（可空）")
    result_label = ask_choice("结果", ("ok", "failed"))
    start = ask("起始时间 YYYY-MM-DD（可空）")
    end = ask("结束时间 YYYY-MM-DD（可空）")

    def fetch(page: int, size: int):
        result = repo.list_operations(
            action=action, actor=actor, result=result_label, start=start, end=end,
            page=page, page_size=size,
        )
        rows = [
            (item["ts"], ACTION_LABELS.get(item["action"], item["action"]), item["actor"],
             item["target"], item["result"], item["detail"])
            for item in result["rows"]
        ]
        return rows, result["pages"]

    _page_loop("操作审计", fetch)


# ------------------------------------------------------------------ 事故 CRUD


def browse_incidents(repo: Repository) -> None:
    keyword = ask("关键字（标题/描述）")
    status = ask_choice("状态", STATUSES)
    severity = ask_choice("级别", SEVERITIES)

    def fetch(page: int, size: int):
        result = repo.list_incidents(
            keyword=keyword, status=status, severity=severity, page=page, page_size=size
        )
        rows = [(item["id"], item["title"], item["severity"], item["status"],
                 item["assignee"], item["updated_at"]) for item in result["rows"]]
        return rows, result["pages"]

    _page_loop("事故记录", fetch)


def incident_menu(repo: Repository) -> None:
    while True:
        print("\n[事故记录] 1 查询  2 新增  3 修改  4 删除  0 返回")
        choice = ask("选择", "0")
        if choice == "1":
            browse_incidents(repo)
        elif choice == "2":
            create_incident(repo)
        elif choice == "3":
            update_incident(repo)
        elif choice == "4":
            delete_incident(repo)
        elif choice == "0":
            return


def create_incident(repo: Repository) -> None:
    title = ask("标题（必填）")
    if not title:
        print("  标题不能为空，已取消。")
        return
    severity = ask_choice("级别", SEVERITIES) or "中"
    status = ask_choice("状态", STATUSES) or "待处理"
    assignee = ask("负责人（可空）")
    description = ask("描述（可空）")
    new_id = repo.create_incident(
        title=title, severity=severity, status=status, assignee=assignee,
        description=description, instance_id=0,
    )
    print("  已新增事故 #{0}：{1}".format(new_id, title))


def update_incident(repo: Repository) -> None:
    incident_id = ask_int("事故 ID")
    if not incident_id:
        print("  已取消。")
        return
    current = repo.get_incident(incident_id)
    if current is None:
        print("  事故 #{0} 不存在。".format(incident_id))
        return
    print("  当前：{0} / {1} / {2}".format(current["title"], current["severity"], current["status"]))
    title = ask("新标题（回车不改）", current["title"])
    severity = ask_choice("新级别（回车不改）", SEVERITIES) or current["severity"]
    status = ask_choice("新状态（回车不改）", STATUSES) or current["status"]
    assignee = ask("新负责人（回车不改）", current["assignee"] or "")
    description = ask("新描述（回车不改）", current["description"] or "")
    repo.update_incident(
        incident_id, title=title, severity=severity, status=status,
        assignee=assignee, description=description,
    )
    print("  事故 #{0} 已更新。".format(incident_id))


def delete_incident(repo: Repository) -> None:
    incident_id = ask_int("要删除的事故 ID")
    if not incident_id:
        print("  已取消。")
        return
    current = repo.get_incident(incident_id)
    if current is None:
        print("  事故 #{0} 不存在。".format(incident_id))
        return
    if ask("确认删除 #{0}「{1}」？输入 yes 确认".format(incident_id, current["title"])).lower() != "yes":
        print("  已取消。")
        return
    repo.delete_incident(incident_id)
    print("  已删除事故 #{0}。".format(incident_id))


# ------------------------------------------------------------------ 基础数据


def browse_catalog(repo: Repository) -> None:
    while True:
        print("\n[基础数据] 1 实例  2 房间  3 用户  4 会话  0 返回")
        choice = ask("选择", "0")
        if choice == "1":
            rows = [(item["id"], item["name"], "{0}:{1}".format(item["host"], item["port"]),
                     item["protocol"], item["started_at"], item["stopped_at"] or "运行中")
                    for item in repo.list_instances()]
            print_table(_TABLE_HEADERS["实例"], rows)
        elif choice == "2":
            rows = [(item["id"], item["name"], "{0}:{1}".format(item["host"], item["port"]),
                     item["created_by"], item["created_at"], item["closed_at"] or "使用中")
                    for item in repo.list_rooms()]
            print_table(_TABLE_HEADERS["房间"], rows)
        elif choice == "3":
            rows = [(item["id"], item["nickname"], item["first_seen"], item["last_seen"],
                     item["note"] or "") for item in repo.list_users()]
            print_table(_TABLE_HEADERS["用户"], rows)
        elif choice == "4":
            rows = [(item["room_name"] or "", item["nickname"], item["addr"], item["joined_at"],
                     item["left_at"] or "在线", item["leave_reason"] or "")
                    for item in repo.list_sessions(active_only=False)]
            print_table(_TABLE_HEADERS["会话"], rows)
        elif choice == "0":
            return


# ------------------------------------------------------------------ 统计与维护


def show_statistics(conn: sqlite3.Connection, repo: Repository) -> None:
    data = overview(conn)
    print("\n--- 统计概览 ---")
    print("运行实例 {0} 个；日志 {1} 条（错误 {2} / 警告 {3}）".format(
        len(repo.list_instances()), data["logs_total"], data["logs_error"], data["logs_warning"]))
    print("聊天消息 {0} 条；操作 {1} 条；事故 {2} 条（未关闭 {3}）".format(
        data["messages_total"], data["operations_total"], data["incidents_total"],
        data["incidents_open"]))
    print("房间 {0} 个（使用中 {1}）；用户 {2} 人；在线会话 {3}".format(
        data["rooms_total"], data["rooms_active"], data["users_total"], data["sessions_active"]))
    print("日志时间范围：{0} ~ {1}".format(data["first_log_ts"] or "-", data["last_log_ts"] or "-"))
    print("\n级别分布：")
    for item in level_counts(conn):
        print("  {0:<10}{1}".format(item["level"], item["count"]))
    print("\n事件类型 Top5：")
    for item in event_type_counts(conn, 5):
        print("  {0:<10}{1}".format(item["event_type"], item["count"]))
    print("\n最近 24 小时每小时日志量：")
    for item in hourly_counts(conn, 24):
        print("  {0}  {1:<4}{2}".format(item["hour"], item["count"], "#" * min(int(item["count"]), 50)))
    print("\n事故状态：")
    for item in incident_summary(conn)["by_status"]:
        print("  {0:<8}{1}".format(item["status"], item["count"]))
    print("\n管理动作 Top5：")
    for item in action_counts(conn, 5):
        print("  {0:<16}{1}".format(ACTION_LABELS.get(item["action"], item["action"]),
                                    item["count"]))


def import_menu(repo: Repository) -> None:
    raw = ask("日志文件路径")
    if not raw:
        return
    try:
        result = import_log_file(repo.conn, raw)
    except (FileNotFoundError, OSError) as exc:
        print("  导入失败：{0}".format(exc))
        return
    print("  已导入 {imported} 行（跳过 {skipped} 行），范围 {first_ts} ~ {last_ts}".format(**result))
    repo.insert_operation(
        "LOG_IMPORT", actor="管理员", target=Path(raw).name, result="ok",
        detail="导入 {0} 行".format(result["imported"]),
    )


def export_menu(repo: Repository) -> None:
    target = ask("导出文件路径", "data/logs-export.csv")
    rows = repo.export_logs()
    if len(rows) > MAX_EXPORT_ROWS:
        print("  结果超过 {0} 条，请先加筛选条件（当前只导出前 {0} 条）。".format(MAX_EXPORT_ROWS))
        rows = rows[:MAX_EXPORT_ROWS]
    if not rows:
        print("  没有可导出的日志。")
        return
    path = Path(target)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["时间", "级别", "类型", "人员", "房间", "内容"])
            for item in rows:
                writer.writerow([item["ts"], item["level"], item["event_type"],
                                 item["actor"], item["room_name"], item["message"]])
    except OSError as exc:
        print("  导出失败：{0}".format(exc))
        return
    print("  已导出 {0} 条到 {1}".format(len(rows), path))


def purge_menu(repo: Repository) -> None:
    before = ask("删除该时间之前的日志，格式 YYYY-MM-DD HH:MM:SS")
    if not before:
        return
    if ask("确认删除？输入 yes 确认").lower() != "yes":
        print("  已取消。")
        return
    try:
        deleted = repo.purge_logs(before)
    except ValueError as exc:
        print("  {0}".format(exc))
        return
    repo.insert_operation("LOG_PURGE", actor="管理员", result="ok",
                          detail="清理 {0} 条（{1} 之前）".format(deleted, before))
    print("  已删除 {0} 条日志。".format(deleted))


# ------------------------------------------------------------------ 演示与入口


def demo(conn: sqlite3.Connection, repo: Repository) -> None:
    """无人值守演示：打印各视图首页与统计，供报告截图复现。"""
    print("=== 运行日志（最新 5 条）===")
    logs = repo.list_logs(page_size=5)["rows"]
    print_table(_TABLE_HEADERS["运行日志"],
                [(r["ts"], r["level"], r["event_type"], r["actor"], r["message"]) for r in logs])
    print("\n=== 聊天记录（最新 5 条）===")
    messages = repo.list_messages(page_size=5)["rows"]
    print_table(_TABLE_HEADERS["聊天记录"],
                [(r["ts"], r["room_name"] or "", r["kind"], r["sender"], r["content"])
                 for r in messages])
    print("\n=== 操作审计（最新 5 条）===")
    operations = repo.list_operations(page_size=5)["rows"]
    print_table(_TABLE_HEADERS["操作审计"],
                [(r["ts"], ACTION_LABELS.get(r["action"], r["action"]), r["actor"], r["target"],
                  r["result"], r["detail"]) for r in operations])
    print("\n=== 事故记录 ===")
    incidents = repo.list_incidents()["rows"]
    print_table(_TABLE_HEADERS["事故记录"],
                [(r["id"], r["title"], r["severity"], r["status"], r["assignee"], r["updated_at"])
                 for r in incidents])
    show_statistics(conn, repo)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="聊天服务器运维日志管理系统（命令行）")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="数据库路径")
    parser.add_argument("--demo", action="store_true", help="打印只读演示后退出")
    parser.add_argument("--stats", action="store_true", help="只打印统计概览")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        path = init_db(args.db)
    except OSError as exc:
        print("无法创建数据库 {0}：{1}".format(args.db, exc))
        return 1
    conn = connect(path)
    repo = Repository(conn)
    try:
        if args.stats or args.demo:
            if args.demo:
                demo(conn, repo)
            else:
                show_statistics(conn, repo)
            return 0
        while True:
            print(MENU)
            choice = ask("请选择", "0")
            if choice == "1":
                browse_logs(repo)
            elif choice == "2":
                browse_messages(repo)
            elif choice == "3":
                browse_operations(repo)
            elif choice == "4":
                incident_menu(repo)
            elif choice == "5":
                show_statistics(conn, repo)
            elif choice == "6":
                browse_catalog(repo)
            elif choice == "7":
                import_menu(repo)
            elif choice == "8":
                export_menu(repo)
            elif choice == "9":
                purge_menu(repo)
            elif choice in ("0", ""):
                print("再见。")
                return 0
            else:
                print("  没有这个选项：{0}".format(choice))
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
