"""GUI 冒烟测试：参数解析、窗口能否构建、启停按钮是否接到服务器。

不验证像素布局（那需要人工看截图），只保证「能打开、能启动、不抛异常」。
没有图形显示的环境（例如无桌面的 CI）会自动跳过窗口测试。
"""

from __future__ import annotations

import logging
import unittest

import tkinter as tk

from src.task1_socket_chat import gui_client, gui_server


class GuiParserTests(unittest.TestCase):
    def test_server_parser_defaults(self) -> None:
        args = gui_server.build_parser().parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 0)  # 0 表示由系统自动分配端口

    def test_client_parser_accepts_port_and_name(self) -> None:
        args = gui_client.build_parser().parse_args(["--port", "9101", "--name", "alice"])
        self.assertEqual(args.port, 9101)
        self.assertEqual(args.name, "alice")


class AddressTests(unittest.TestCase):
    def test_display_address_keeps_explicit_host(self) -> None:
        self.assertEqual(gui_server.display_address("127.0.0.1", 51234), "127.0.0.1:51234")

    def test_display_address_wildcard_mentions_lan(self) -> None:
        text = gui_server.display_address("0.0.0.0", 51234)
        self.assertIn(":51234", text)
        self.assertIn("0.0.0.0", text)

    def test_parse_address_accepts_host_port(self) -> None:
        self.assertEqual(gui_client.parse_address("127.0.0.1:51234"), ("127.0.0.1", 51234))

    def test_parse_address_accepts_port_only(self) -> None:
        self.assertEqual(gui_client.parse_address(" 51234 "), (None, 51234))

    def test_parse_address_accepts_host_only(self) -> None:
        self.assertEqual(gui_client.parse_address("10.0.0.8"), ("10.0.0.8", None))
        self.assertEqual(gui_client.parse_address(""), (None, None))


class GuiWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # 没有可用显示时跳过，而不是让整个测试套件失败
            self.skipTest(f"当前环境没有图形显示: {exc}")
        self.root.withdraw()

    def tearDown(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_server_window_builds_and_starts(self) -> None:
        app = gui_server.ServerApp(self.root, "127.0.0.1", 0)
        self.addCleanup(logging.getLogger("task1").removeHandler, app.handler)

        port = app.start_server()
        self.assertIsNotNone(app.server)
        self.assertIsInstance(port, int)
        assert port is not None  # 供类型检查器与读者确认：下面要按整数端口断言
        self.assertGreater(port, 0)
        self.assertIn(str(port), app.status_var.get())
        self.assertIn(f"127.0.0.1:{port}", app.address_var.get())
        self.assertEqual(app.start_server(), port)  # 重复点「创建聊天室」应返回同一端口，不重开服务
        self.assertTrue(app.copy_button.instate(["!disabled"]))
        app.copy_address()
        self.assertEqual(self.root.clipboard_get(), f"127.0.0.1:{port}")
        app.stop_server()
        self.assertIsNone(app.server)
        self.assertEqual(app.status_var.get(), "未启动")
        self.assertIn("（启动后自动分配", app.address_var.get())

    def test_client_window_builds(self) -> None:
        app = gui_client.ClientApp(self.root, "127.0.0.1", 9000, "alice")
        self.assertEqual(app.status_var.get(), "未连接")
        self.assertFalse(app.online)
        self.assertEqual(app.target_var.get(), "发送给：所有人（群聊）")


if __name__ == "__main__":
    unittest.main()
