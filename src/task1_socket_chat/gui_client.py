"""Tkinter 聊天客户端：群聊、私聊、在线列表，与命令行版共用同一套协议。

界面只负责显示与收集输入；真正的 socket 收发仍在 :class:`~.client.ChatClient`
的后台线程里，消息通过 ``on_message`` 回调转交给 Tk 主线程渲染。
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from .client import ChatClient
from .config import DEFAULT_HOST, DEFAULT_PORT, MAX_TEXT_LENGTH, ClientConfig
from .protocol import Message, ProtocolError


class ClientApp:
    """聊天客户端窗口。"""

    def __init__(self, root: tk.Tk, host: str, port: int, name: str) -> None:
        self.root = root
        self.client: ChatClient | None = None
        self.inbox: queue.Queue[tuple[str, object]] = queue.Queue()
        self.private_target: str | None = None
        self.online = False
        self.host = host
        self.port = port
        self.nickname = name

        self.host_var = tk.StringVar(value=host)
        self.port_var = tk.StringVar(value=str(port))
        self.name_var = tk.StringVar(value=name)
        self.status_var = tk.StringVar(value="未连接")
        self.target_var = tk.StringVar(value="发送给：所有人（群聊）")

        self._build_ui()
        self._poll_inbox()

    # ---------------------------------------------------------------- 界面

    def _build_ui(self) -> None:
        self.root.title("任务1 · 聊天客户端")
        self.root.geometry("980x640")
        self.root.minsize(760, 480)

        toolbar = ttk.Frame(self.root, padding=(10, 8))
        toolbar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(toolbar, text="服务器").pack(side=tk.LEFT)
        self.host_entry = ttk.Entry(toolbar, textvariable=self.host_var, width=14)
        self.host_entry.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(toolbar, text="端口").pack(side=tk.LEFT)
        self.port_entry = ttk.Entry(toolbar, textvariable=self.port_var, width=7)
        self.port_entry.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(toolbar, text="昵称").pack(side=tk.LEFT)
        self.name_entry = ttk.Entry(toolbar, textvariable=self.name_var, width=12)
        self.name_entry.pack(side=tk.LEFT, padx=(4, 12))
        self.connect_button = ttk.Button(toolbar, text="连接", command=self.connect)
        self.connect_button.pack(side=tk.LEFT)
        self.disconnect_button = ttk.Button(
            toolbar, text="断开", command=self.disconnect, state=tk.DISABLED
        )
        self.disconnect_button.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(toolbar, textvariable=self.status_var, foreground="#0a7d32").pack(
            side=tk.LEFT, padx=(16, 0)
        )

        # 底部输入区与提示先占位，再让聊天区吃掉剩余高度，否则输入框会被挤出窗口
        input_bar = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        input_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(input_bar, textvariable=self.target_var, width=32).pack(side=tk.LEFT)
        self.entry = ttk.Entry(input_bar, font=("Microsoft YaHei UI", 10))
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        self.entry.bind("<Return>", lambda _event: self.send())
        ttk.Button(input_bar, text="发送", command=self.send).pack(side=tk.LEFT)

        ttk.Label(
            self.root,
            text="提示：在右侧选中一位用户即可私聊；不选则群聊。回车发送。",
            foreground="#777",
            padding=(10, 0, 10, 6),
        ).pack(side=tk.BOTTOM, fill=tk.X)

        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))

        chat_frame = ttk.Labelframe(paned, text="聊天记录", padding=8)
        paned.add(chat_frame, weight=3)
        chat_scroll = ttk.Scrollbar(chat_frame)
        chat_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat_text = tk.Text(
            chat_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Microsoft YaHei UI", 10),
            yscrollcommand=chat_scroll.set,
            padx=8,
            pady=6,
        )
        self.chat_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        chat_scroll.config(command=self.chat_text.yview)
        self.chat_text.tag_configure("self", foreground="#1565c0")
        self.chat_text.tag_configure("other", foreground="#212121")
        self.chat_text.tag_configure("private", foreground="#7b1fa2")
        self.chat_text.tag_configure("system", foreground="#757575")
        self.chat_text.tag_configure("error", foreground="#c62828")

        users_frame = ttk.Labelframe(paned, text="在线用户", padding=8)
        paned.add(users_frame, weight=1)
        users_buttons = ttk.Frame(users_frame)
        users_buttons.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        ttk.Button(users_buttons, text="刷新", command=self.refresh_users).pack(side=tk.LEFT)
        ttk.Button(users_buttons, text="群聊", command=self.clear_target).pack(side=tk.LEFT, padx=6)
        users_scroll = ttk.Scrollbar(users_frame)
        users_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.user_list = tk.Listbox(
            users_frame,
            height=14,
            exportselection=False,
            font=("Microsoft YaHei UI", 10),
            yscrollcommand=users_scroll.set,
        )
        self.user_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        users_scroll.config(command=self.user_list.yview)
        self.user_list.bind("<<ListboxSelect>>", self._on_user_selected)
        self.user_list.bind("<Double-1>", self._on_user_selected)

    # ---------------------------------------------------------------- 连接

    def connect(self) -> None:
        if self.client is not None:
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("端口无效", "端口必须是 1–65535 之间的整数")
            return
        if not 1 <= port <= 65535:
            messagebox.showerror("端口无效", "端口必须是 1–65535 之间的整数")
            return
        self.host = self.host_var.get().strip() or DEFAULT_HOST
        self.port = port
        name = self.name_var.get().strip() or "guest"
        self.nickname = name
        self.status_var.set("连接中…")
        self.connect_button.config(state=tk.DISABLED)
        config = ClientConfig(host=self.host, port=self.port, name=name)
        threading.Thread(target=self._connect_worker, args=(config,), name="gui-connect", daemon=True).start()

    def _connect_worker(self, config: ClientConfig) -> None:
        """在工作线程完成可能阻塞的连接与握手，结果通过队列回到界面线程。"""
        client = ChatClient(config)
        client.on_message = lambda message: self.inbox.put(("message", message))
        client.on_disconnect = lambda reason: self.inbox.put(("disconnect", reason))
        client.echo = False
        try:
            assigned = client.connect()
        except (ConnectionError, ProtocolError) as exc:
            self.inbox.put(("connect_failed", str(exc)))
            return
        client.start_receiver()
        self.client = client
        self.inbox.put(("connected", assigned))
        try:
            client.request_users()
        except ConnectionError:
            pass

    def disconnect(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            client.quit()
        self._set_offline("已断开连接")
        self._append("* 已断开与服务器的连接", "system")

    def _set_offline(self, reason: str) -> None:
        self.online = False
        self.status_var.set(reason)
        self.connect_button.config(state=tk.NORMAL)
        self.disconnect_button.config(state=tk.DISABLED)
        self.user_list.delete(0, tk.END)
        self.clear_target()

    def _on_connected(self, assigned: str) -> None:
        self.online = True
        self.nickname = assigned
        self.name_var.set(assigned)
        self.status_var.set(f"已连接 · {assigned} @ {self.host}:{self.port}")
        self.disconnect_button.config(state=tk.NORMAL)
        self._append(f"* 已连接 {self.host}:{self.port}，昵称 {assigned}", "system")

    def _on_connect_failed(self, reason: str) -> None:
        self._set_offline("未连接")
        self._append(f"! 连接失败: {reason}", "error")
        messagebox.showerror("连接失败", reason)

    def _on_disconnected(self, reason: str) -> None:
        if not self.online:
            return
        self.client = None
        self._set_offline("连接已断开")
        self._append(f"! {reason}", "error")

    # ---------------------------------------------------------------- 收发

    def send(self) -> None:
        client = self.client
        if client is None or not client.connected:
            self._append("! 尚未连接服务器", "error")
            return
        text = self.entry.get().strip()
        if not text:
            return
        if len(text) > MAX_TEXT_LENGTH:
            self._append(f"! 消息过长（上限 {MAX_TEXT_LENGTH} 字符）", "error")
            return
        stamp = time.strftime("%H:%M:%S")
        try:
            if self.private_target:
                client.send_private(self.private_target, text)
                self._append(f"[{stamp}] 我 → {self.private_target}: {text}", "self")
            else:
                client.send_chat(text)
                self._append(f"[{stamp}] {client.name}: {text}", "self")
        except ConnectionError as exc:
            self._append(f"! 发送失败: {exc}", "error")
            return
        self.entry.delete(0, tk.END)

    def refresh_users(self) -> None:
        client = self.client
        if client is None or not client.connected:
            return
        try:
            client.request_users()
        except ConnectionError as exc:
            self._append(f"! 刷新失败: {exc}", "error")

    def _on_user_selected(self, _event: tk.Event) -> None:
        selection = self.user_list.curselection()
        if not selection:
            return
        name = str(self.user_list.get(selection[0]))
        if name == self.nickname:
            self._append("* 不能私聊自己，已切回群聊", "system")
            self.clear_target()
            return
        self.private_target = name
        self.target_var.set(f"发送给：{name}（私聊）")

    def clear_target(self) -> None:
        self.private_target = None
        self.target_var.set("发送给：所有人（群聊）")
        self.user_list.selection_clear(0, tk.END)

    # ---------------------------------------------------------------- 渲染

    def _render_message(self, message: Message) -> None:
        kind = message.type
        stamp = time.strftime("%H:%M:%S", time.localtime(message.ts))
        if kind == "chat":
            self._append(f"[{stamp}] {message.sender}: {message.text}", "other")
        elif kind == "private":
            self._append(f"[{stamp}] {message.sender} 私聊你: {message.text}", "private")
        elif kind == "system":
            self._append(f"* {message.text}", "system")
            users = message.data.get("users")
            if isinstance(users, list):
                self._set_users([str(user) for user in users])
        elif kind == "error":
            self._append(f"! 服务器: {message.text}", "error")
        elif kind == "userlist":
            users = [str(user) for user in message.data.get("users", [])]
            self._set_users(users)
            joined = "、".join(users) if users else "（无）"
            self._append(f"在线用户: {joined}", "system")
        elif kind == "welcome":
            self._append(f"* 已连接，昵称: {message.text}", "system")

    def _set_users(self, users: list[str]) -> None:
        self.user_list.delete(0, tk.END)
        for user in users:
            self.user_list.insert(tk.END, user)
        if self.private_target is not None and self.private_target not in users:
            self.clear_target()

    def _append(self, line: str, tag: str) -> None:
        self.chat_text.config(state=tk.NORMAL)
        self.chat_text.insert(tk.END, line.rstrip() + "\n", tag)
        self.chat_text.see(tk.END)
        self.chat_text.config(state=tk.DISABLED)

    def _poll_inbox(self) -> None:
        while True:
            try:
                kind, payload = self.inbox.get_nowait()
            except queue.Empty:
                break
            if kind == "message" and isinstance(payload, Message):
                self._render_message(payload)
            elif kind == "connected":
                self._on_connected(str(payload))
            elif kind == "connect_failed":
                self._on_connect_failed(str(payload))
            elif kind == "disconnect":
                self._on_disconnected(str(payload))
        self.root.after(80, self._poll_inbox)

    def on_close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            client.quit()
        self.root.destroy()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="任务1 聊天客户端（图形界面）")
    parser.add_argument("--host", default=DEFAULT_HOST, help="服务器地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="服务器端口")
    parser.add_argument("--name", default="guest", help="昵称，重名时服务器会自动加后缀")
    return parser


def bring_to_front(root: tk.Tk) -> None:
    """把窗口提到最前。

    从终端启动时新窗口常常被终端挡住，用户会以为「没反应」；这里短暂置顶并抢焦点，
    800ms 后取消置顶，避免一直压住其它窗口。
    """
    root.lift()
    root.focus_force()
    root.attributes("-topmost", True)
    root.after(800, lambda: root.attributes("-topmost", False))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = tk.Tk()
    app = ClientApp(root, args.host, args.port, args.name)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    bring_to_front(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
