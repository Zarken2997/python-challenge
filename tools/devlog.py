#!/usr/bin/env python3
"""开发日志（docs/DEVLOG.md）维护工具，仅依赖标准库。

子命令
------
new    在日志最上方插入一条带占位符的新条目
check  校验条目格式、必填字段、时间倒序（供提交前与 CI 使用）
stats  统计条目数、任务分布与最近记录

示例
----
    python tools/devlog.py new --task T1 --title "服务端多客户端支持"
    python tools/devlog.py check
    python tools/devlog.py stats --limit 5
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = REPO_ROOT / "docs" / "DEVLOG.md"
MARKER = "<!-- ENTRIES -->"
TASKS = ("T0", "T1", "T2", "T3", "META")
REQUIRED = ("目标", "改动", "验证", "下一步")
OPTIONAL = ("决策", "问题")
PLACEHOLDERS = {"", "TODO", "TBD", "待填写", "..."}
PLACEHOLDER_PREFIXES = ("TODO", "TBD")

HEADER = """# 开发日志（DEVLOG）

> 项目：Python 挑战课题（Socket 通信 / 数据库 MIS / MNIST 识别）
> 规则见 `AGENTS.md` 第 7 节：**最新条目在最上方**，追加式，不得删改历史条目。
> `目标` / `改动` / `验证` / `下一步` 为必填字段；提交前运行 `python tools/devlog.py check`。

<!-- ENTRIES -->
"""

HEADING_RE = re.compile(
    r"^##\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2})\s+·\s+(?P<task>[A-Z0-9]+)\s+·\s+(?P<title>.+?)\s*$"
)
BULLET_RE = re.compile(r"^-\s*(?P<key>[^:：]+?)\s*[:：]\s*(?P<value>.*)$")
ENTRY_TEMPLATE = """## {stamp} · {task} · {title}
- 目标: TODO
- 改动: TODO
- 验证: TODO（命令 → 关键输出）
- 决策: 
- 问题: 无
- 下一步: TODO
"""


def read_text(path: Path) -> str:
    """按 UTF-8 读取，允许 BOM；文件不存在时返回空串。"""
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""


def write_text(path: Path, text: str) -> None:
    # 3.7 的 Path.write_text 没有 newline 参数，这里显式用 open 保证写出的换行是 LF。
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def parse_entries(lines: list[str]) -> list[dict]:
    """把 Markdown 行拆成条目列表，每条含起始行号、标题信息与字段。"""
    entries: list[dict] = []
    current: dict | None = None
    for index, line in enumerate(lines):
        heading = HEADING_RE.match(line)
        if heading:
            if current is not None:
                entries.append(current)
            current = {
                "line": index + 1,
                "date": heading.group("date"),
                "time": heading.group("time"),
                "task": heading.group("task"),
                "title": heading.group("title"),
                "fields": {},
                "bullets": 0,
                "lines": 1,
            }
            continue
        if line.startswith("## "):
            # 其他二级标题：结束当前条目，避免误吞
            if current is not None:
                entries.append(current)
                current = None
            continue
        if current is not None:
            current["lines"] += 1
            bullet = BULLET_RE.match(line)
            if bullet:
                current["bullets"] += 1
                current["fields"][bullet.group("key").strip()] = bullet.group("value").strip()
    if current is not None:
        entries.append(current)
    return entries


def pending_required_fields(entry: dict) -> list[str]:
    """返回该条目中仍为占位符（空、TODO、TBD…）的必填字段名。"""
    pending: list[str] = []
    for key in REQUIRED:
        value = entry["fields"].get(key, "")
        if value in PLACEHOLDERS or value.upper().startswith(PLACEHOLDER_PREFIXES):
            pending.append(key)
    return pending


def format_entry(stamp: datetime, task: str, title: str) -> str:
    """按模板生成条目文本（字段值为占位符，需人工补全）。"""
    return ENTRY_TEMPLATE.format(stamp=stamp.strftime("%Y-%m-%d %H:%M"), task=task, title=title.strip())


def insert_entry_text(text: str, entry: str) -> str:
    """把条目插到 MARKER 下方最上方；无标记时追加到文件末尾，返回新全文。

    纯函数：不读写文件，便于在沙箱或单元测试中直接验证。
    """
    lines = text.splitlines()
    if MARKER in lines:
        position = lines.index(MARKER) + 1
        # 跳过标记后的空行，保证条目紧贴标记下方
        while position < len(lines) and not lines[position].strip():
            position += 1
        lines[position:position] = entry.splitlines() + [""]
    else:
        lines += [""] + entry.splitlines() + [""]
    return "\n".join(lines).rstrip() + "\n"


def check_entries(text: str) -> tuple[list[str], list[str], list[dict]]:
    """返回 (errors, warnings, entries)。"""
    errors: list[str] = []
    warnings: list[str] = []
    lines = text.splitlines()

    if MARKER not in text:
        warnings.append(f"缺少插入标记 {MARKER}，`new` 将追加到文件末尾")
    if not text.strip():
        errors.append("日志文件为空，请先写入标题与插入标记")

    entries = parse_entries(lines)
    if not entries and text.strip():
        errors.append("未找到任何条目，标题格式应为：## 2026-11-23 01:00 · T1 · 标题")

    previous: datetime | None = None
    for entry in entries:
        where = f"第 {entry['line']} 行"
        try:
            stamp = datetime.strptime(f"{entry['date']} {entry['time']}", "%Y-%m-%d %H:%M")
        except ValueError:
            errors.append(f"{where}：时间戳无效 {entry['date']} {entry['time']}")
            stamp = None

        if entry["task"] not in TASKS:
            errors.append(f"{where}：任务编号 {entry['task']} 不合法，允许 {', '.join(TASKS)}")
        if not entry["title"] or entry["title"].startswith("TODO"):
            errors.append(f"{where}：标题为空或仍为占位符")

        for key in REQUIRED:
            value = entry["fields"].get(key)
            if value is None:
                errors.append(f"{where}：缺少必填字段「{key}」")
            elif value in PLACEHOLDERS or value.upper().startswith(PLACEHOLDER_PREFIXES):
                errors.append(f"{where}：「{key}」仍为占位符（{value or '空'}）")

        for key in entry["fields"]:
            if key not in REQUIRED and key not in OPTIONAL:
                warnings.append(f"{where}：未知字段「{key}」，允许 {', '.join(REQUIRED + OPTIONAL)}")

        if entry["bullets"] == 0:
            errors.append(f"{where}：条目内没有任何 `- 字段: 值` 行")
        if entry["lines"] > 40:
            warnings.append(f"{where}：条目 {entry['lines']} 行，建议压缩到 30 行内")
        if stamp and previous and stamp > previous:
            errors.append(f"{where}：时间戳 {entry['date']} {entry['time']} 晚于上一条，日志应为最新在上")
        if stamp:
            previous = stamp

    return errors, warnings, entries


def cmd_check(args: argparse.Namespace) -> int:
    path: Path = args.file
    text = read_text(path)
    if not text.strip():
        print(f"[错误] 找不到日志或内容为空：{path}")
        return 1

    errors, warnings, entries = check_entries(text)
    for message in warnings:
        print(f"[警告] {message}")
    for message in errors:
        print(f"[错误] {message}")

    if errors:
        print(f"\n结果：{len(entries)} 条记录，{len(errors)} 个错误，{len(warnings)} 个警告")
        return 1

    latest = entries[0] if entries else None
    print(f"[通过] {len(entries)} 条记录，{len(warnings)} 个警告：{path}")
    if latest:
        print(f"       最新：{latest['date']} {latest['time']} · {latest['task']} · {latest['title']}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    path: Path = args.file
    text = read_text(path)
    if not text.strip():
        text = HEADER

    if args.task not in TASKS:
        print(f"[错误] 任务编号 {args.task} 不合法，允许 {', '.join(TASKS)}", file=sys.stderr)
        return 2
    if not args.title.strip():
        print("[错误] --title 不能为空", file=sys.stderr)
        return 2

    if args.now:
        try:
            stamp = datetime.strptime(args.now, "%Y-%m-%d %H:%M")
        except ValueError:
            print("[错误] --now 格式应为 'YYYY-MM-DD HH:MM'", file=sys.stderr)
            return 2
    else:
        stamp = datetime.now().replace(second=0, microsecond=0)

    entries = parse_entries(text.splitlines())
    if entries and not args.force:
        pending = pending_required_fields(entries[0])
        if pending:
            print(
                f"[错误] 最新条目仍有未填字段（{', '.join(pending)}），"
                f"先补完或用 --force 跳过：第 {entries[0]['line']} 行 · {entries[0]['title']}",
                file=sys.stderr,
            )
            return 1

    entry = format_entry(stamp, args.task, args.title)
    write_text(path, insert_entry_text(text, entry))
    print(f"[新增] {entry.splitlines()[0]}")
    print("       请补全 TODO 字段后再运行：python tools/devlog.py check")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    path: Path = args.file
    text = read_text(path)
    entries = parse_entries(text.splitlines())
    if not entries:
        print(f"[提示] {path} 中没有条目")
        return 0

    counter = Counter(entry["task"] for entry in entries)
    total_lines = len(text.splitlines())
    print(f"文件：{path}")
    print(f"条目：{len(entries)} 条，{total_lines} 行（归档阈值 500 行）")
    print("分布：" + "  ".join(f"{task}={counter[task]}" for task in TASKS if counter[task]))
    newest, oldest = entries[0], entries[-1]
    print(f"最新：{newest['date']} {newest['time']} · {newest['task']} · {newest['title']}")
    print(f"最早：{oldest['date']} {oldest['time']} · {oldest['task']} · {oldest['title']}")
    print(f"最近 {min(args.limit, len(entries))} 条：")
    for entry in entries[: args.limit]:
        print(f"  - {entry['date']} {entry['time']} · {entry['task']} · {entry['title']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devlog.py",
        description="维护 docs/DEVLOG.md：新增条目、校验格式、统计进度",
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_LOG, help=f"日志路径（默认 {DEFAULT_LOG}）")
    sub = parser.add_subparsers(dest="command", required=True)

    new_parser = sub.add_parser("new", help="在最上方插入一条新条目")
    new_parser.add_argument("--task", required=True, help=f"任务编号：{', '.join(TASKS)}")
    new_parser.add_argument("--title", required=True, help="条目标题（≤30 字）")
    new_parser.add_argument("--now", help="指定时间 'YYYY-MM-DD HH:MM'（默认当前时间）")
    new_parser.add_argument("--force", action="store_true", help="即使上一条未填完也插入")
    new_parser.set_defaults(func=cmd_new)

    check_parser = sub.add_parser("check", help="校验格式与必填字段")
    check_parser.set_defaults(func=cmd_check)

    stats_parser = sub.add_parser("stats", help="统计条目分布与最近记录")
    stats_parser.add_argument("--limit", type=int, default=3, help="显示最近几条（默认 3）")
    stats_parser.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
