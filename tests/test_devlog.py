#!/usr/bin/env python3
"""tools/devlog.py 的单元测试：格式校验、占位符拦截、条目插入位置。

全部在内存中完成：不建临时目录、不落盘，因此在受限沙箱中也能稳定运行。
文件读写由 `mock.patch.object` 替换，只验证解析、校验与插入逻辑。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import devlog  # noqa: E402  （需先把仓库根目录加入 sys.path）

HEADER = """# 开发日志（DEVLOG）

> 规则见 `AGENTS.md` 第 7 节。

<!-- ENTRIES -->
"""

VALID_ENTRY = """## 2026-11-23 01:00 · T1 · 客户端同时收发
- 目标: 验证客户端能同时收发消息
- 改动: `src/task1_socket_chat/client.py`（新增）
- 验证: `python -m unittest` → 4 passed
- 决策: 用 threading 而非 asyncio，因为课程未讲事件循环
- 问题: 无
- 下一步: 增加长度前缀分帧
"""


def make_entry(stamp: str = "2026-11-23 01:00", task: str = "T1", title: str = "示例条目") -> str:
    """构造一条字段完整的条目文本。"""
    return (
        f"## {stamp} · {task} · {title}\n"
        "- 目标: 验证插入顺序\n"
        "- 改动: `tools/devlog.py`\n"
        "- 验证: `python -m unittest` → passed\n"
        "- 下一步: 补文档\n"
    )


class CheckEntriesTest(unittest.TestCase):
    """check_entries：纯文本校验。"""

    def test_valid_log_passes(self) -> None:
        errors, warnings, entries = devlog.check_entries(HEADER + "\n" + VALID_ENTRY)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["task"], "T1")
        self.assertEqual(entries[0]["fields"]["决策"], "用 threading 而非 asyncio，因为课程未讲事件循环")

    def test_missing_required_field(self) -> None:
        broken = VALID_ENTRY.replace("- 改动: `src/task1_socket_chat/client.py`（新增）\n", "")
        errors, _, _ = devlog.check_entries(HEADER + "\n" + broken)
        self.assertTrue(any("缺少必填字段「改动」" in message for message in errors), errors)

    def test_placeholder_rejected(self) -> None:
        broken = VALID_ENTRY.replace("验证客户端能同时收发消息", "TODO")
        errors, _, _ = devlog.check_entries(HEADER + "\n" + broken)
        self.assertTrue(any("「目标」仍为占位符" in message for message in errors), errors)

    def test_invalid_task_tag(self) -> None:
        broken = VALID_ENTRY.replace("· T1 ·", "· T9 ·")
        errors, _, _ = devlog.check_entries(HEADER + "\n" + broken)
        self.assertTrue(any("任务编号 T9 不合法" in message for message in errors), errors)

    def test_newest_first_violation(self) -> None:
        older = make_entry("2026-11-22 23:00", "T0", "更早的条目")
        # 上一条 11-22 23:00，下一条 11-23 23:00 属于"新的写在下面"，应报错
        later = make_entry("2026-11-23 23:00", "T2", "更晚的条目")
        errors, _, _ = devlog.check_entries(HEADER + "\n" + older + "\n" + later)
        self.assertTrue(any("晚于上一条" in message for message in errors), errors)

    def test_unknown_field_is_warning_only(self) -> None:
        text = HEADER + "\n" + VALID_ENTRY + "- 备注: 额外内容\n"
        errors, warnings, _ = devlog.check_entries(text)
        self.assertEqual(errors, [])
        self.assertTrue(any("未知字段「备注」" in message for message in warnings), warnings)

    def test_empty_log_is_error(self) -> None:
        errors, _, _ = devlog.check_entries("")
        self.assertTrue(any("日志文件为空" in message for message in errors), errors)


class PendingFieldsTest(unittest.TestCase):
    """pending_required_fields：判断新骨架是否仍需填。"""

    def test_template_is_pending(self) -> None:
        entry = devlog.format_entry(datetime(2026, 11, 23, 1, 0), "T1", "标题")
        parsed = devlog.parse_entries(entry.splitlines())[0]
        self.assertEqual(devlog.pending_required_fields(parsed), ["目标", "改动", "验证", "下一步"])

    def test_filled_entry_is_clear(self) -> None:
        parsed = devlog.parse_entries(VALID_ENTRY.splitlines())[0]
        self.assertEqual(devlog.pending_required_fields(parsed), [])


class InsertEntryTextTest(unittest.TestCase):
    """insert_entry_text：纯函数插入位置。"""

    def test_inserts_below_marker_above_existing(self) -> None:
        existing = make_entry("2026-11-22 10:00", "T0", "旧条目")
        new = make_entry("2026-11-23 02:00", "META", "新条目")
        result = devlog.insert_entry_text(HEADER + "\n" + existing, new)

        marker_at = result.index(devlog.MARKER)
        new_at = result.index("新条目")
        old_at = result.index("旧条目")
        self.assertLess(marker_at, new_at)
        self.assertLess(new_at, old_at)

    def test_keeps_existing_content_and_passes_check(self) -> None:
        existing = make_entry("2026-11-22 10:00", "T0", "旧条目")
        result = devlog.insert_entry_text(HEADER + "\n" + existing, make_entry("2026-11-23 02:00"))
        self.assertTrue(result.startswith("# 开发日志（DEVLOG）"))
        parsed = devlog.parse_entries(result.splitlines())
        self.assertEqual(len(parsed), 2)
        errors, _, _ = devlog.check_entries(result)
        self.assertEqual(errors, [])

    def test_appends_when_marker_missing(self) -> None:
        result = devlog.insert_entry_text("# 日志\n", make_entry("2026-11-23 02:00", "META", "无标记"))
        self.assertIn("无标记", result)
        self.assertTrue(result.rstrip().endswith("- 下一步: 补文档"))


class CmdNewTest(unittest.TestCase):
    """cmd_new：用内存存储替换文件读写，验证插入与拒绝逻辑。"""

    def setUp(self) -> None:
        self.store = {"text": ""}
        self.path = pathlib.Path("docs/DEVLOG.md")  # 仅作标识，不实际访问
        patcher_read = mock.patch.object(devlog, "read_text", lambda path: self.store["text"])
        patcher_write = mock.patch.object(
            devlog, "write_text", lambda path, text: self.store.update(text=text)
        )
        patcher_read.start()
        patcher_write.start()
        self.addCleanup(patcher_read.stop)
        self.addCleanup(patcher_write.stop)

    def run_new(self, *extra: str) -> int:
        argv = ["--file", str(self.path), "new", "--task", "T1", "--title", "新条目", "--now", "2026-11-23 02:00"]
        return devlog.main(argv + list(extra))

    def test_creates_file_with_header(self) -> None:
        self.assertEqual(self.run_new(), 0)
        text = self.store["text"]
        self.assertTrue(text.startswith("# 开发日志（DEVLOG）"))
        self.assertIn("2026-11-23 02:00 · T1 · 新条目", text)

    def test_refuses_while_top_entry_unfilled(self) -> None:
        self.store["text"] = HEADER + "\n" + make_entry("2026-11-22 10:00", "T0", "旧条目").replace(
            "验证插入顺序", "TODO"
        )
        before = self.store["text"]
        self.assertEqual(self.run_new(), 1)
        self.assertEqual(self.store["text"], before)

    def test_force_inserts_despite_pending(self) -> None:
        self.store["text"] = HEADER + "\n" + devlog.format_entry(datetime(2026, 11, 23, 1, 0), "T0", "骨架")
        self.assertEqual(self.run_new("--force"), 0)
        self.assertLess(self.store["text"].index("新条目"), self.store["text"].index("骨架"))

    def test_rejects_invalid_task(self) -> None:
        argv = ["--file", str(self.path), "new", "--task", "T9", "--title", "x"]
        self.assertEqual(devlog.main(argv), 2)


if __name__ == "__main__":
    unittest.main()
