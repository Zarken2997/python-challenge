"""GUI 冒烟测试：参数解析、窗口能否构建、启停按钮是否接到服务器。

不验证像素布局（那需要人工看截图），只保证「能打开、能启动、不抛异常」。
没有图形显示的环境（例如无桌面的 CI）会自动跳过窗口测试。
"""

from __future__ import annotations

import logging
import socket
import unittest

import tkinter as tk

from src.task1_socket_chat import gui_client, gui_server


class GuiParserTests(unittest.TestCase):
    def test_server_parser_defaults(self) -> None:
        args = gui_server.build_parser().parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 9000)

    def test_client_parser_accepts_port_and_name(self) -> None:
        args = gui_client.build_parser().parse_args(["--port", "9101", "--name", "alice"])
        self.assertEqual(args.port, 9101)
        self.assertEqual(args.name, "alice")


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
        app = gui_server.ServerApp(self.root, "127.0.0.1", 9000)
        self.addCleanup(logging.getLogger("task1").removeHandler, app.handler)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        app.port_var.set(str(free_port))
        app.start_server()
        self.assertIsNotNone(app.server)
        self.assertIn(str(free_port), app.status_var.get())
        app.stop_server()
        self.assertIsNone(app.server)
        self.assertEqual(app.status_var.get(), "未启动")

    def test_client_window_builds(self) -> None:
        app = gui_client.ClientApp(self.root, "127.0.0.1", 9000, "alice")
        self.assertEqual(app.status_var.get(), "未连接")
        self.assertFalse(app.online)
        self.assertEqual(app.target_var.get(), "发送给：所有人（群聊）")


if __name__ == "__main__":
    unittest.main()
