"""GUI 冒烟测试：参数解析、窗口能否构建、注册中心启停与列表刷新是否接通。

不验证像素布局（那需要人工看截图），只保证「能打开、能启动、列表能刷新、不抛异常」。
没有图形显示的环境（例如无桌面的 CI）会自动跳过窗口测试。
"""

from __future__ import annotations

import logging
import unittest

import tkinter as tk

from src.task1_socket_chat import gui_client, gui_server
from src.task1_socket_chat.config import DEFAULT_PORT


class GuiParserTests(unittest.TestCase):
    def test_server_parser_defaults(self) -> None:
        args = gui_server.build_parser().parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, DEFAULT_PORT)  # 控制端口默认 9000

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

    def test_monitor_window_starts_hub_and_lists_rooms(self) -> None:
        app = gui_server.MonitorApp(self.root, "127.0.0.1", 0)
        self.addCleanup(logging.getLogger("task1").removeHandler, app.handler)

        port = app.start_hub()
        self.assertIsNotNone(app.hub)
        self.assertIsInstance(port, int)
        assert port is not None  # 供类型检查器与读者确认：下面要按整数端口断言
        self.assertGreater(port, 0)
        self.assertIn(str(port), app.status_var.get())
        # 端口输入框要同步成真实端口，不能在界面上仍显示 0
        self.assertEqual(app.port_var.get(), str(port))
        self.assertTrue(app.port_entry.instate(["disabled"]))
        self.assertTrue(app.host_entry.instate(["disabled"]))
        self.assertEqual(app.start_hub(), port)  # 重复点「启动」应返回同一端口，不重开服务

        room = app.hub.create_room("测试房")
        app._refresh_all_now()
        room_iid = str(room.room_id)
        self.assertIn(room_iid, app.room_tree.get_children())
        values = app.room_tree.item(room_iid, "values")
        self.assertEqual(values[0], f"127.0.0.1:{room.port}")

        request = app.hub.submit_request("审批房", "alice", lambda message: None)
        app._refresh_all_now()
        self.assertIn(str(request.request_id), app.request_tree.get_children())

        app.stop_hub()
        self.assertIsNone(app.hub)
        self.assertEqual(app.status_var.get(), "未启动")
        # 停止后输入框恢复可编辑，保留刚用过的端口便于原地址重启
        self.assertTrue(app.port_entry.instate(["!disabled"]))
        self.assertEqual(app.port_var.get(), str(port))

    def test_client_window_builds(self) -> None:
        app = gui_client.ClientApp(self.root, "127.0.0.1", 9000, "alice")
        self.assertEqual(app.hub_var.get(), "127.0.0.1:9000")
        self.assertEqual(app.status_var.get(), "注册中心：未连接")
        self.assertEqual(app.room_var.get(), "当前聊天室：未加入")
        self.assertFalse(app.online)

    def test_client_schedules_retry_when_hub_unreachable(self) -> None:
        app = gui_client.ClientApp(self.root, "127.0.0.1", 9000, "alice")
        app._hub_target = ("127.0.0.1", 9000)
        # 直接投递一次失败事件，避免依赖真实网络与定时器
        app.inbox.put(("hub_failed", "[WinError 10061] 无法连接"))
        app._poll_inbox()

        self.assertEqual(app._hub_retries, 1)
        self.assertIn("重试", app.status_var.get())
        text = app.chat_text.get("1.0", tk.END)
        self.assertIn("启动注册中心", text)  # 提示用户先启动服务器

    def test_client_stops_auto_retry_after_disconnect(self) -> None:
        app = gui_client.ClientApp(self.root, "127.0.0.1", 9000, "alice")
        app.disconnect_hub()
        self.assertEqual(app._hub_retries, gui_client.MAX_HUB_RETRIES)
        self.assertEqual(app.status_var.get(), "注册中心：已断开")


if __name__ == "__main__":
    unittest.main()
