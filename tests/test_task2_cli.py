from __future__ import annotations

import io
import sys
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path

from src.task2_db_system import cli, importer
from src.task2_db_system.db import connect, init_db
from tests.test_task2_db import DATA_DIR, cleanup_sqlite, temp_db_path


def write_log(lines: str) -> Path:
    path = DATA_DIR / "test-log-{0}.log".format(uuid.uuid4().hex[:10])
    path.write_text(lines, encoding="utf-8")
    return path


class ImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = temp_db_path("import")
        self.addCleanup(cleanup_sqlite, self.db_path)
        self.db_path = init_db(self.db_path)
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)

    def _import(self, text: str) -> dict:
        path = write_log(text)
        self.addCleanup(path.unlink)
        return importer.import_log_file(self.conn, path)

    def test_parses_two_timestamp_formats(self) -> None:
        parsed = importer.parse_line("2026-11-20 10:00:00,123 INFO    监听 127.0.0.1:9000", "2026-01-01")
        assert parsed is not None
        self.assertEqual(parsed["ts"], "2026-11-20 10:00:00")
        self.assertEqual(parsed["message"], "监听 127.0.0.1:9000")
        short = importer.parse_line("10:00:05 WARNING 心跳超时", "2026-11-20")
        assert short is not None
        self.assertEqual(short["ts"], "2026-11-20 10:00:05")
        self.assertIsNone(importer.parse_line("   \n", "2026-11-20"))

    def test_import_counts_and_classification(self) -> None:
        result = self._import(
            "2026-11-20 10:00:00,123 INFO    服务端已启动\n"
            "2026-11-20 10:00:05 INFO    小明 加入了聊天室\n"
            "2026-11-20 10:00:06 ERROR   握手失败：对方拒绝连接\n"
            "这行没有时间戳\n"
            "\n"
        )
        self.assertEqual(result["imported"], 4)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["first_ts"], "2026-11-20 10:00:00")
        self.assertGreater(result["instance_id"], 0)

        rows = self.conn.execute(
            "SELECT event_type, level, message FROM log_event ORDER BY id"
        ).fetchall()
        self.assertEqual([row["event_type"] for row in rows], ["启动", "加入", "错误", "其他"])
        self.assertEqual(rows[2]["level"], "ERROR")

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            importer.import_log_file(self.conn, DATA_DIR / "no-such-log-file.log")

    def test_empty_file_imports_nothing(self) -> None:
        result = self._import("")
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["skipped"], 0)


class CliSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = temp_db_path("cli")
        self.addCleanup(cleanup_sqlite, self.db_path)

    def _run(self, argv, stdin_text: str = "") -> str:
        buffer = io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            with redirect_stdout(buffer):
                code = cli.main(argv)
        finally:
            sys.stdin = old_stdin
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_demo_prints_sections(self) -> None:
        self._seed()
        output = self._run(["--db", str(self.db_path), "--demo"])
        for section in ("运行日志", "聊天记录", "操作审计", "事故记录", "统计"):
            self.assertIn(section, output)

    def test_menu_quits_on_blank_input(self) -> None:
        output = self._run(["--db", str(self.db_path)], "")
        self.assertIn("聊天服务器运维日志管理系统", output)

    def test_stats_flag(self) -> None:
        self._seed()
        output = self._run(["--db", str(self.db_path), "--stats"])
        self.assertIn("统计概览", output)

    def _seed(self) -> None:
        from src.task2_db_system.seed import seed

        seed(self.db_path, reset=True)


if __name__ == "__main__":
    unittest.main()
