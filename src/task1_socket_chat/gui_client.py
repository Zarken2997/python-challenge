"""Tkinter 聊天客户端：先连注册中心看房间，再加入某个聊天室聊天。

界面分三块：

- 左侧「聊天室」：注册中心里所有房间（名称 / 地址 / 人数），可刷新、申请开房；
- 中间「聊天记录」：加入房间后的群聊与私聊，按类型着色；
- 右侧「在线用户」：只显示当前房间里的成员，选中即可私聊。

客户端启动时会自动连接注册中心（控制端口），因此不需要手动敲地址就能看到房间列表；
「加入聊天室」后才真正连接到该房间自己的端口——相同「IP:端口」的人才会在同一个房间。
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

if __package__ in (None, ""):
    # PyCharm 的绿色三角按「脚本」方式运行本文件时包内相对导入会失败；
    # 这里把仓库根目录加入 sys.path 并补上包名，`python -m ...` 与直接运行都可用。
    from pathlib import Path

    _REPO_ROOT = Path(__file__).resolve().parents[2]
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    __package__ = "src.task1_socket_chat"

from .client import ChatClient
from .config import DEFAULT_HOST, DEFAULT_PORT, MAX_TEXT_LENGTH, ClientConfig
from .hub import HubClient
from .protocol import Message, ProtocolError


class ClientApp:
    """聊天客户端窗口：注册中心 + 聊天室。"""

    def __init__(self, root: tk.Tk, host: str, port: int, name: str) -> None:
        self.root = root
        self.hub_client: HubClient | None = None
        self.client: ChatClient | None = None
        self.current_room: dict[str, object] | None = None
        self.inbox: queue.Queue[tuple[str, object]] = queue.Queue()
        self.private_target: str | None = None
        self.online = False
        self.host = host
        self.port = port
        self.nickname = name

        self.hub_var = tk.StringVar(value=f"{host}:{port}")
        self.name_var = tk.StringVar(value=name)
        self.status_var = tk.StringVar(value="注册中心：未连接")
        self.room_var = tk.StringVar(value="当前聊天室：未加入")
        self.target_var = tk.StringVar(value="发送给：所有人（群聊）")

        self._build_ui()
        self._poll_inbox()
        self.root.after(200, self.connect_hub)

    # ---------------------------------------------------------------- 界面

    def _build_ui(self) -> None:
        self.root.title("任务1 · 聊天客户端（先选聊天室）")
        self.root.geometry("1180x680")
        self.root.minsize(900, 520)

        toolbar = ttk.Frame(self.root, padding=(10, 8))
        toolbar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(toolbar, text="注册中心").pack(side=tk.LEFT)
        self.hub_entry = ttk.Entry(toolbar, textvariable=self.hub_var, width=18)
        self.hub_entry.pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(toolbar, text="昵称").pack(side=tk.LEFT)
        self.name_entry = ttk.Entry(toolbar, textvariable=self.name_var, width=10)
        self.name_entry.pack(side=tk.LEFT, padx=(4, 12))
        self.hub_button = ttk.Button(toolbar, text="连接注册中心", command=self.connect_hub)
        self.hub_button.pack(side=tk.LEFT)
        self.hub_disconnect_button = ttk.Button(
            toolbar, text="断开注册中心", command=self.disconnect_hub, state=tk.DISABLED
        )
        self.hub_disconnect_button.pack(side=tk.LEFT, padx=(6, 0))
        self.status_label = ttk.Label(toolbar, textvariable=self.status_var, foreground="#0a7d32")
        self.status_label.pack(side=tk.LEFT, padx=(14, 0))

        room_bar = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        room_bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(room_bar, textvariable=self.room_var, foreground="#c62828").pack(side=tk.LEFT)
        self.leave_button = ttk.Button(
            room_bar, text="退出聊天室", command=self.leave_room, state=tk.DISABLED
        )
        self.leave_button.pack(side=tk.RIGHT)

        # 底部输入区与提示先占位，再让聊天区吃掉剩余高度，否则输入框会被挤出窗口
        input_bar = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        input_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(input_bar, textvariable=self.target_var, width=30).pack(side=tk.LEFT)
        self.entry = ttk.Entry(input_bar, font=("Microsoft YaHei UI", 10))
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        self.entry.bind("<Return>", lambda _event: self.send())
        self.send_button = ttk.Button(input_bar, text="发送", command=self.send, state=tk.DISABLED)
        self.send_button.pack(side=tk.LEFT)

        ttk.Label(
            self.root,
            text="提示：先连注册中心 → 选中房间「加入聊天室」；相同「IP:端口」的人才会进入同一个房间；"
            "右侧选中用户即可私聊，回车发送。",
            foreground="#777",
            padding=(10, 0, 10, 6),
        ).pack(side=tk.BOTTOM, fill=tk.X)

        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))
        self.paned = paned
        self.root.after(120, self._place_sashes)

        rooms_frame = ttk.Labelframe(paned, text="聊天室（来自注册中心）", padding=8)
        paned.add(rooms_frame, weight=2)
        room_buttons = ttk.Frame(rooms_frame)
        room_buttons.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        ttk.Button(room_buttons, text="加入聊天室", command=self.join_selected_room).pack(side=tk.LEFT)
        ttk.Button(room_buttons, text="申请开房…", command=self.request_room).pack(side=tk.LEFT, padx=6)
        ttk.Button(room_buttons, text="刷新", command=self.refresh_rooms).pack(side=tk.LEFT)
        self.room_tree = ttk.Treeview(
            rooms_frame,
            columns=("address", "users"),
            show="tree headings",
            height=14,
        )
        self.room_tree.heading("#0", text="房间名")
        self.room_tree.heading("address", text="地址（双击加入）")
        self.room_tree.heading("users", text="人数")
        self.room_tree.column("#0", width=90, anchor=tk.W)
        self.room_tree.column("address", width=140, anchor=tk.W)
        self.room_tree.column("users", width=45, anchor=tk.CENTER)
        self.room_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.room_tree.bind("<Double-1>", lambda _event: self.join_selected_room())

        chat_frame = ttk.Labelframe(paned, text="聊天记录", padding=8)
        paned.add(chat_frame, weight=4)
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

    # ---------------------------------------------------------------- 注册中心

    def connect_hub(self) -> None:
        if self.hub_client is not None:
            return
        host, port = parse_address(self.hub_var.get())
        if port is None:
            port = DEFAULT_PORT
        self.hub_var.set(f"{host or DEFAULT_HOST}:{port}")
        name = self.name_var.get().strip() or "guest"
        self.status_var.set("注册中心：连接中…")
        self.hub_button.config(state=tk.DISABLED)
        threading.Thread(
            target=self._connect_hub_worker,
            args=(host or DEFAULT_HOST, port, name),
            name="gui-hub-connect",
            daemon=True,
        ).start()

    def _connect_hub_worker(self, host: str, port: int, name: str) -> None:
        client = HubClient(host=host, port=port, name=name)
        client.on_message = lambda message: self.inbox.put(("hub_message", message))
        client.on_disconnect = lambda reason: self.inbox.put(("hub_disconnect", reason))
        try:
            client.connect()
        except (OSError, ProtocolError, ConnectionError) as exc:
            self.inbox.put(("hub_failed", str(exc)))
            return
        client.start_receiver()
        self.hub_client = client
        self.inbox.put(("hub_connected", client.nickname))

    def disconnect_hub(self) -> None:
        client, self.hub_client = self.hub_client, None
        if client is not None:
            client.close()
        self.status_var.set("注册中心：已断开")
        self.hub_button.config(state=tk.NORMAL)
        self.hub_disconnect_button.config(state=tk.DISABLED)
        self.room_tree.delete(*self.room_tree.get_children())

    def refresh_rooms(self) -> None:
        client = self.hub_client
        if client is None or not client.connected:
            self._append("! 尚未连接注册中心", "error")
            return
        client.request_rooms()

    def request_room(self) -> None:
        client = self.hub_client
        if client is None or not client.connected:
            messagebox.showinfo("未连接", "请先连接注册中心")
            return
        name = simpledialog.askstring("申请开房", "房间名（留空由服务器命名）：", parent=self.root)
        if name is None:
            return
        client.request_room(name.strip())
        self._append(f"* 已提交开房申请「{name.strip() or '自动命名'}」，等待服务器批准", "system")

    def _on_hub_message(self, message: Message) -> None:
        if message.type == "rooms":
            self._set_rooms(message.data.get("rooms") or [])
        elif message.type == "room_created":
            room = message.data.get("room") or {}
            self._append(
                f"* 服务器已批准开房：「{room.get('name')}」→ {room.get('address')}", "system"
            )
            self._set_rooms([room] if room else [])
            self.refresh_rooms()
        elif message.type == "room_rejected":
            self._append(f"! 开房申请被拒绝：{message.text}", "error")
            messagebox.showinfo("申请未通过", message.text, parent=self.root)
        elif message.type == "error":
            self._append(f"! 注册中心: {message.text}", "error")

    def _set_rooms(self, rooms: list[dict[str, object]]) -> None:
        existing = set(self.room_tree.get_children())
        wanted = {str(room.get("id")) for room in rooms}
        for iid in existing - wanted:
            self.room_tree.delete(iid)
        for room in rooms:
            iid = str(room.get("id"))
            values = (str(room.get("address")), str(room.get("users")))
            if iid in existing:
                self.room_tree.item(iid, text=str(room.get("name")), values=values)
            else:
                self.room_tree.insert("", tk.END, iid=iid, text=str(room.get("name")), values=values)

    # ---------------------------------------------------------------- 聊天室

    def join_selected_room(self) -> None:
        selection = self.room_tree.selection()
        if not selection:
            messagebox.showinfo("未选择", "请先在左侧选择一个聊天室")
            return
        values = self.room_tree.item(selection[0], "values")
        if not values:
            return
        host, port = parse_address(str(values[0]))
        if port is None:
            messagebox.showerror("地址无效", "聊天室地址缺少端口，请刷新房间列表")
            return
        name = self.room_tree.item(selection[0], "text")
        self.join_room(host or DEFAULT_HOST, port, name)

    def join_room(self, host: str, port: int, room_name: str) -> None:
        """连接某个聊天室的端口；已在房间里会先退出旧房间。"""
        if self.client is not None:
            self.leave_room()
        self.host = host
        self.port = port
        self.current_room = {"name": room_name, "host": host, "port": port}
        self.room_var.set(f"当前聊天室：{room_name}（{host}:{port}）")
        self._append(f"* 正在加入聊天室「{room_name}」→ {host}:{port}", "system")
        config = ClientConfig(host=host, port=port, name=self.name_var.get().strip() or "guest")
        threading.Thread(
            target=self._connect_worker, args=(config,), name="gui-room-connect", daemon=True
        ).start()

    def leave_room(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            client.quit()
        self.online = False
        self.current_room = None
        self.room_var.set("当前聊天室：未加入")
        self.send_button.config(state=tk.DISABLED)
        self.leave_button.config(state=tk.DISABLED)
        self.user_list.delete(0, tk.END)
        self.clear_target()

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

    def _set_offline(self, reason: str) -> None:
        self.online = False
        self.send_button.config(state=tk.DISABLED)
        self.leave_button.config(state=tk.DISABLED)
        self.user_list.delete(0, tk.END)
        self.clear_target()
        if self.current_room is not None:
            self._append(f"! {reason}", "error")

    def _on_connected(self, assigned: str) -> None:
        self.online = True
        self.nickname = assigned
        self.name_var.set(assigned)
        self.disconnect_note = f"{assigned} @ {self.host}:{self.port}"
        self.send_button.config(state=tk.NORMAL)
        self.leave_button.config(state=tk.NORMAL)
        self._append(f"* 已加入 {self.current_room.get('name') if self.current_room else ''}"
                     f"（{self.host}:{self.port}），昵称 {assigned}", "system")

    def _on_connect_failed(self, reason: str) -> None:
        self._set_offline("连接失败")
        self._append(f"! 连接聊天室失败: {reason}", "error")
        messagebox.showerror("连接失败", reason)

    def _on_disconnected(self, reason: str) -> None:
        if not self.online:
            return
        self.client = None
        self._set_offline(f"与聊天室的连接已断开: {reason}")

    # ---------------------------------------------------------------- 收发

    def send(self) -> None:
        client = self.client
        if client is None or not client.connected:
            self._append("! 尚未加入聊天室", "error")
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

    def _place_sashes(self) -> None:
        """窗口显示后按比例摆好三栏，避免房间地址被挤到看不见。

        PanedWindow 默认按子控件请求宽度分配，房间列表列多时会把地址截断；
        这里显式给出左栏与右栏的像素宽度。
        """
        try:
            width = self.paned.winfo_width()
            if width < 400:
                return
            self.paned.sashpos(0, max(340, int(width * 0.29)))
            self.paned.sashpos(1, width - 210)
        except tk.TclError:  # 窗口尚未映射或已销毁时忽略
            return

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
            elif kind == "hub_message" and isinstance(payload, Message):
                self._on_hub_message(payload)
            elif kind == "hub_connected":
                self._on_hub_connected(str(payload))
            elif kind == "hub_failed":
                self.status_var.set("注册中心：连接失败")
                self.hub_button.config(state=tk.NORMAL)
                self._append(f"! 连接注册中心失败: {payload}", "error")
            elif kind == "hub_disconnect":
                self.hub_client = None
                self.status_var.set("注册中心：连接已断开")
                self.hub_button.config(state=tk.NORMAL)
                self.hub_disconnect_button.config(state=tk.DISABLED)
            elif kind == "connected":
                self._on_connected(str(payload))
            elif kind == "connect_failed":
                self._on_connect_failed(str(payload))
            elif kind == "disconnect":
                self._on_disconnected(str(payload))
        self.root.after(80, self._poll_inbox)

    def _on_hub_connected(self, nickname: str) -> None:
        self.nickname = nickname
        self.name_var.set(nickname)
        self.status_var.set(f"注册中心：已连接 · {nickname}")
        self.hub_button.config(state=tk.DISABLED)
        self.hub_disconnect_button.config(state=tk.NORMAL)
        self._append(f"* 已连接注册中心，昵称 {nickname}", "system")
        client = self.hub_client
        if client is not None:
            self._set_rooms(client.rooms)
            client.request_rooms()

    def on_close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            client.quit()
        hub, self.hub_client = self.hub_client, None
        if hub is not None:
            hub.close()
        self.root.destroy()


def parse_address(text: str) -> tuple[str | None, int | None]:
    """解析服务器地址，支持 `127.0.0.1:54321`、只填端口 `54321` 或只填主机。

    返回 `(host, port)`，未提供的部分为 None；这样用户可以把服务器窗口里
    「复制地址」得到的 `IP:端口` 直接粘贴进「注册中心」输入框。
    """
    text = text.strip()
    if not text:
        return None, None
    if text.isdigit():
        return None, int(text)
    host, sep, port_text = text.partition(":")
    if sep and port_text.isdigit():
        return host or None, int(port_text)
    return text, None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="任务1 聊天客户端（图形界面，支持多聊天室）")
    parser.add_argument("--host", default=DEFAULT_HOST, help="注册中心地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="注册中心控制端口")
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
