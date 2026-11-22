"""Tkinter 多聊天室监控台：一个窗口管理所有房间、审批开房申请、查看终端日志。

界面结构：

- 顶部：控制端口配置 + 启动/停止注册中心 + 全服公告；
- 左栏：聊天室列表（创建/关闭/复制地址）、待审批的开房申请（批准/拒绝）、
  所有房间的在线用户（踢出）；
- 右栏：「终端监控」把 ``task1`` 的日志实时镜像到窗口，和命令行输出一致。

服务器端与客户端严格分开：本窗口只做服务器侧管理，不参与聊天输入；
客户端见 :mod:`gui_client`。所有网络动作都在后台线程，界面只读快照，
因此这里的方法可以直接从 Tk 主线程调用。
"""

from __future__ import annotations

import argparse
import logging
import queue
import socket
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

if __package__ in (None, ""):
    # PyCharm 的绿色三角按「脚本」方式运行本文件时包内相对导入会失败；
    # 这里把仓库根目录加入 sys.path 并补上包名，`python -m ...` 与直接运行都可用。
    from pathlib import Path

    _REPO_ROOT = Path(__file__).resolve().parents[2]
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    __package__ = "src.task1_socket_chat"

from .config import DEFAULT_HOST, DEFAULT_PORT, HubConfig
from .hub import ChatHub

LOGGER = logging.getLogger("task1.hub")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
LOG_DATEFMT = "%H:%M:%S"


class QueueLogHandler(logging.Handler):
    """把日志记录放进线程安全队列，由 Tk 主线程定时取走渲染。

    服务器在多个线程里写日志，Tk 控件只能在主线程操作，
    所以这里只做入队，绝不在工作线程里碰界面。
    """

    def __init__(self, sink: queue.Queue[tuple[str, str]]) -> None:
        super().__init__()
        self.sink = sink
        self.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.put_nowait((record.levelname, self.format(record)))
        except queue.Full:  # 队列无界，正常不会发生；真满了就丢弃而不是拖垮服务器线程
            self.handleError(record)


def format_duration(seconds: int) -> str:
    """把秒数格式化成 ``1h02m03s`` / ``02m03s`` / ``03s``。"""
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes:02d}m{secs:02d}s"
    return f"{secs:02d}s"


def display_address(host: str, port: int) -> str:
    """把绑定地址转成可以直接填到客户端里的「IP:端口」。

    监听 0.0.0.0 时用户没法直接输入 *，换成这台机器的局域网 IP 并提示。
    """
    if host in ("0.0.0.0", ""):
        try:
            lan_ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            return f"<本机局域网IP>:{port}（监听 0.0.0.0）"
        return f"{lan_ip}:{port}（监听 0.0.0.0，局域网可连）"
    return f"{host}:{port}"


class MonitorApp:
    """多聊天室监控台窗口。"""

    def __init__(self, root: tk.Tk, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.root = root
        self.hub: ChatHub | None = None
        self._bound_port: int | None = None
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.handler = QueueLogHandler(self.log_queue)
        self.handler.setLevel(logging.INFO)

        logger = logging.getLogger("task1")
        logger.setLevel(logging.INFO)
        logger.addHandler(self.handler)

        self.host_var = tk.StringVar(value=host)
        self.port_var = tk.StringVar(value=str(port))
        self.status_var = tk.StringVar(value="未启动")
        self.count_var = tk.StringVar(value="聊天室 0 个 · 在线 0 人")
        self.notice_var = tk.StringVar()
        self.auto_scroll = tk.BooleanVar(value=True)

        self._build_ui()
        self._refresh_all()
        self._poll_logs()

    # ---------------------------------------------------------------- 界面

    def _build_ui(self) -> None:
        self.root.title("任务1 · 多聊天室监控台（服务器端）")
        self.root.geometry("1240x780")
        self.root.minsize(1040, 640)

        toolbar = ttk.Frame(self.root, padding=(10, 8))
        toolbar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(toolbar, text="控制端口").pack(side=tk.LEFT)
        self.host_entry = ttk.Entry(toolbar, textvariable=self.host_var, width=13)
        self.host_entry.pack(side=tk.LEFT, padx=(4, 8))
        ttk.Label(toolbar, text="端口（0 = 自动）").pack(side=tk.LEFT)
        self.port_entry = ttk.Entry(toolbar, textvariable=self.port_var, width=7)
        self.port_entry.pack(side=tk.LEFT, padx=(4, 10))
        self.start_button = ttk.Button(toolbar, text="启动注册中心", command=self.start_hub)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(
            toolbar, text="停止注册中心", command=self.stop_hub, state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=(6, 0))
        self.status_label = ttk.Label(toolbar, textvariable=self.status_var, foreground="#0a7d32")
        self.status_label.pack(side=tk.LEFT, padx=(14, 0))

        notice_bar = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        notice_bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(notice_bar, text="全服公告").pack(side=tk.LEFT)
        self.notice_entry = ttk.Entry(notice_bar, textvariable=self.notice_var)
        self.notice_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        self.notice_entry.bind("<Return>", lambda _event: self.broadcast_all())
        self.notice_button = ttk.Button(
            notice_bar, text="发送到所有聊天室", command=self.broadcast_all, state=tk.DISABLED
        )
        self.notice_button.pack(side=tk.LEFT)

        status_bar = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(status_bar, textvariable=self.count_var).pack(side=tk.LEFT)
        ttk.Label(
            status_bar,
            text="提示：双击房间可关闭，双击用户可踢出",
            foreground="#777",
        ).pack(side=tk.RIGHT)

        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))

        left = ttk.Frame(paned)
        paned.add(left, weight=2)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=3)
        left.rowconfigure(3, weight=1)
        left.rowconfigure(5, weight=3)

        # ---- 聊天室列表
        rooms_frame = ttk.Labelframe(left, text="聊天室（每个房间一个端口）", padding=6)
        rooms_frame.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self.room_tree = ttk.Treeview(
            rooms_frame,
            columns=("address", "users"),
            show="tree headings",
            height=8,
        )
        self.room_tree.heading("#0", text="名称")
        self.room_tree.heading("address", text="地址（客户端填这个）")
        self.room_tree.heading("users", text="人数")
        self.room_tree.column("#0", width=100, anchor=tk.W)
        self.room_tree.column("address", width=170, anchor=tk.W)
        self.room_tree.column("users", width=50, anchor=tk.CENTER)
        self.room_tree.pack(fill=tk.BOTH, expand=True)
        self.room_tree.bind("<Double-1>", lambda _event: self.close_selected_room())

        room_buttons = ttk.Frame(rooms_frame)
        room_buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(room_buttons, text="创建聊天室…", command=self.create_room).pack(side=tk.LEFT)
        ttk.Button(room_buttons, text="关闭选中", command=self.close_selected_room).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(room_buttons, text="复制地址", command=self.copy_selected_address).pack(
            side=tk.LEFT
        )

        # ---- 开房申请
        requests_frame = ttk.Labelframe(left, text="开房申请（客户端提交，需服务器批准）", padding=6)
        requests_frame.grid(row=2, column=0, sticky="nsew")
        self.request_tree = ttk.Treeview(
            requests_frame,
            columns=("requester", "name", "time"),
            show="headings",
            height=3,
        )
        for column, title, width, anchor in (
            ("requester", "申请人", 100, tk.W),
            ("name", "申请房间名", 140, tk.W),
            ("time", "申请时间", 90, tk.CENTER),
        ):
            self.request_tree.heading(column, text=title)
            self.request_tree.column(column, width=width, anchor=anchor)
        self.request_tree.pack(fill=tk.BOTH, expand=True)

        request_buttons = ttk.Frame(requests_frame)
        request_buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(request_buttons, text="批准选中", command=self.approve_selected_request).pack(
            side=tk.LEFT
        )
        ttk.Button(request_buttons, text="拒绝选中", command=self.reject_selected_request).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(request_buttons, text="刷新", command=self._refresh_all_now).pack(side=tk.LEFT)

        # ---- 在线用户
        users_frame = ttk.Labelframe(left, text="所有聊天室的在线用户", padding=6)
        users_frame.grid(row=4, column=0, rowspan=2, sticky="nsew")
        self.user_tree = ttk.Treeview(
            users_frame,
            columns=("room", "duration"),
            show="tree headings",
            height=8,
        )
        self.user_tree.heading("#0", text="昵称")
        self.user_tree.heading("room", text="聊天室")
        self.user_tree.heading("duration", text="时长")
        self.user_tree.column("#0", width=110, anchor=tk.W)
        self.user_tree.column("room", width=120, anchor=tk.W)
        self.user_tree.column("duration", width=80, anchor=tk.CENTER)
        self.user_tree.pack(fill=tk.BOTH, expand=True)
        self.user_tree.bind("<Double-1>", lambda _event: self.kick_selected_user())

        user_buttons = ttk.Frame(users_frame)
        user_buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(user_buttons, text="踢出选中", command=self.kick_selected_user).pack(side=tk.LEFT)

        # ---- 终端监控
        right = ttk.Labelframe(paned, text="终端监控（与命令行输出一致）", padding=8)
        paned.add(right, weight=3)

        log_bar = ttk.Frame(right)
        log_bar.pack(fill=tk.X, pady=(0, 6))
        ttk.Checkbutton(log_bar, text="自动滚动", variable=self.auto_scroll).pack(side=tk.LEFT)
        ttk.Button(log_bar, text="清空", command=self.clear_log).pack(side=tk.RIGHT)
        ttk.Button(log_bar, text="保存日志…", command=self.save_log).pack(side=tk.RIGHT, padx=6)

        text_frame = ttk.Frame(right)
        text_frame.pack(fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text = tk.Text(
            text_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 10),
            yscrollcommand=scrollbar.set,
            background="#111418",
            foreground="#d6dde6",
            insertbackground="#d6dde6",
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.log_text.yview)
        self.log_text.tag_configure("warning", foreground="#f0b429")
        self.log_text.tag_configure("error", foreground="#ff6b6b")
        self.log_text.tag_configure("notice", foreground="#4fc3f7")
        self.log_text.tag_configure("info", foreground="#d6dde6")

    # ---------------------------------------------------------------- 生命周期

    def start_hub(self) -> int | None:
        """启动注册中心（控制端口），返回实际端口；端口 0 表示自动分配。"""
        if self.hub is not None:
            return self._bound_port
        host = self.host_var.get().strip() or DEFAULT_HOST
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("端口无效", "端口必须是 0–65535 之间的整数（0 表示自动分配）")
            return None
        if not 0 <= port <= 65535:
            messagebox.showerror("端口无效", "端口必须是 0–65535 之间的整数（0 表示自动分配）")
            return None

        hub = ChatHub(HubConfig(host=host, port=port))
        try:
            bound_host, bound_port = hub.start()
        except OSError as exc:
            self._append(f"! 启动失败: {exc}", "error")
            messagebox.showerror("启动失败", f"无法监听 {host}:{port}\n{exc}")
            return None

        self.hub = hub
        self._bound_port = bound_port
        self.port_var.set(str(bound_port))
        self.host_entry.config(state=tk.DISABLED)
        self.port_entry.config(state=tk.DISABLED)
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.notice_button.config(state=tk.NORMAL)
        self.status_var.set(f"注册中心 {display_address(bound_host, bound_port)}")
        self._append(
            f"* 注册中心已启动 → {bound_host}:{bound_port}"
            "（客户端连这个端口查看/申请聊天室）",
            "notice",
        )
        return bound_port

    def stop_hub(self) -> None:
        hub, self.hub = self.hub, None
        if hub is not None:
            hub.shutdown()
            self._append("* 注册中心已停止，所有聊天室与客户端连接已关闭", "notice")
        self._bound_port = None
        self.status_var.set("未启动")
        self.host_entry.config(state=tk.NORMAL)
        self.port_entry.config(state=tk.NORMAL)
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.notice_button.config(state=tk.DISABLED)
        self._refresh_all_now()

    def on_close(self) -> None:
        self.stop_hub()
        logging.getLogger("task1").removeHandler(self.handler)
        self.root.destroy()

    # ---------------------------------------------------------------- 房间操作

    def create_room(self) -> None:
        if self.hub is None:
            messagebox.showinfo("未启动", "请先启动注册中心")
            return
        name = simpledialog.askstring(
            "创建聊天室", "房间名（留空自动命名）：", parent=self.root
        )
        if name is None:
            return
        try:
            room = self.hub.create_room(name.strip())
        except RuntimeError as exc:
            messagebox.showerror("创建失败", str(exc))
            return
        self._append(f"* 已创建聊天室「{room.name}」→ {room.address}", "notice")
        self._refresh_all_now()

    def close_selected_room(self) -> None:
        if self.hub is None:
            messagebox.showinfo("未启动", "请先启动注册中心")
            return
        selection = self.room_tree.selection()
        if not selection:
            messagebox.showinfo("未选择", "请先在左侧选择一个聊天室")
            return
        room_id = int(selection[0])
        room = self.hub.get_room(room_id)
        if room is None:
            return
        if not messagebox.askyesno(
            "确认关闭", f"关闭「{room.name}」（{room.address}）？房内用户会被通知并断开。"
        ):
            return
        if not self.hub.close_room(room_id):
            self._append(f"! 关闭失败：聊天室 {room_id} 已不存在", "warning")
        self._refresh_all_now()

    def copy_selected_address(self) -> None:
        selection = self.room_tree.selection()
        if not selection or self.hub is None:
            messagebox.showinfo("未选择", "请先在左侧选择一个聊天室")
            return
        room = self.hub.get_room(int(selection[0]))
        if room is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(room.address)
        self._append(f"* 已复制聊天室地址 {room.address}", "notice")

    def kick_selected_user(self) -> None:
        if self.hub is None:
            messagebox.showinfo("未启动", "请先启动注册中心")
            return
        selection = self.user_tree.selection()
        if not selection:
            messagebox.showinfo("未选择", "请先在下方选择一个在线用户")
            return
        room_id_text, _, name = selection[0].partition("|")
        if not messagebox.askyesno("确认踢出", f"确定把 {name} 请出该聊天室吗？"):
            return
        if not self.hub.kick(int(room_id_text), name):
            self._append(f"! 踢出失败：{name} 已不在线", "warning")
        self._refresh_all_now()

    def broadcast_all(self) -> None:
        if self.hub is None:
            messagebox.showinfo("未启动", "请先启动注册中心")
            return
        text = self.notice_var.get().strip()
        if not text:
            messagebox.showinfo("公告为空", "请输入要发送到所有聊天室的内容")
            return
        delivered = self.hub.broadcast_all(text)
        self._append(f"* 全服公告已送达 {delivered} 人：{text}", "notice")
        self.notice_var.set("")

    # ---------------------------------------------------------------- 审批

    def approve_selected_request(self) -> None:
        if self.hub is None:
            messagebox.showinfo("未启动", "请先启动注册中心")
            return
        selection = self.request_tree.selection()
        if not selection:
            messagebox.showinfo("未选择", "请先选择一条开房申请")
            return
        room = self.hub.approve_request(int(selection[0]))
        if room is None:
            self._append("! 批准失败（申请可能已处理或房间重名）", "warning")
        else:
            self._append(f"* 已批准申请，聊天室「{room.name}」→ {room.address}", "notice")
        self._refresh_all_now()

    def reject_selected_request(self) -> None:
        if self.hub is None:
            messagebox.showinfo("未启动", "请先启动注册中心")
            return
        selection = self.request_tree.selection()
        if not selection:
            messagebox.showinfo("未选择", "请先选择一条开房申请")
            return
        reason = simpledialog.askstring(
            "拒绝申请", "给申请人的理由：", initialvalue="服务器暂不批准开房", parent=self.root
        )
        if reason is None:
            return
        if self.hub.reject_request(int(selection[0]), reason.strip() or "服务器未批准该申请"):
            self._append(f"* 已拒绝申请并回复：{reason}", "notice")
        self._refresh_all_now()

    # ---------------------------------------------------------------- 日志

    def clear_log(self) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def save_log(self) -> None:
        content = self.log_text.get("1.0", tk.END).strip()
        if not content:
            messagebox.showinfo("没有内容", "当前没有可保存的日志")
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="保存监控日志",
            defaultextension=".log",
            initialfile=time.strftime("chat-hub-%Y%m%d-%H%M%S.log"),
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content + "\n")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._append(f"* 日志已保存到 {path}", "notice")

    def _append(self, line: str, tag: str = "info") -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, line.rstrip() + "\n", tag)
        if self.auto_scroll.get():
            self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _poll_logs(self) -> None:
        while True:
            try:
                level, line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append(line, level.lower())
        self.root.after(150, self._poll_logs)

    # ---------------------------------------------------------------- 刷新

    def _refresh_all_now(self) -> None:
        self._refresh_rooms()
        self._refresh_requests()
        self._refresh_users()

    def _refresh_all(self) -> None:
        self._refresh_all_now()
        self.root.after(800, self._refresh_all)

    def _refresh_rooms(self) -> None:
        hub = self.hub
        rooms = hub.rooms_snapshot() if hub is not None else []
        existing = set(self.room_tree.get_children())
        wanted = {str(room["id"]) for room in rooms}
        for iid in existing - wanted:
            self.room_tree.delete(iid)
        for room in rooms:
            iid = str(room["id"])
            values = (
                display_address(str(room["host"]), int(room["port"])),
                str(room["users"]),
            )
            if iid in existing:
                self.room_tree.item(iid, text=str(room["name"]), values=values)
            else:
                self.room_tree.insert("", tk.END, iid=iid, text=str(room["name"]), values=values)

    def _refresh_requests(self) -> None:
        hub = self.hub
        requests = hub.pending_requests() if hub is not None else []
        existing = set(self.request_tree.get_children())
        wanted = {str(item["id"]) for item in requests}
        for iid in existing - wanted:
            self.request_tree.delete(iid)
        for item in requests:
            iid = str(item["id"])
            values = (
                str(item["requester"]),
                str(item["name"]) or "（自动命名）",
                time.strftime("%H:%M:%S", time.localtime(float(item["created_at"]))),
            )
            if iid in existing:
                self.request_tree.item(iid, values=values)
            else:
                self.request_tree.insert("", tk.END, iid=iid, values=values)

    def _refresh_users(self) -> None:
        hub = self.hub
        users = hub.online_users() if hub is not None else []
        existing = set(self.user_tree.get_children())
        wanted = {f'{user["room_id"]}|{user["name"]}' for user in users}
        for iid in existing - wanted:
            self.user_tree.delete(iid)
        for user in users:
            iid = f'{user["room_id"]}|{user["name"]}'
            values = (
                str(user["room"]),
                format_duration(int(user["connected_seconds"])),
            )
            if iid in existing:
                self.user_tree.item(iid, values=values)
            else:
                self.user_tree.insert("", tk.END, iid=iid, text=str(user["name"]), values=values)
        room_count = hub.room_count if hub is not None else 0
        self.count_var.set(f"聊天室 {room_count} 个 · 在线 {len(users)} 人")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="任务1 多聊天室监控台（服务器端图形界面）")
    parser.add_argument("--host", default=DEFAULT_HOST, help="控制端口监听地址，局域网演示用 0.0.0.0")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="控制端口（默认 9000）；填 0 表示由系统自动分配，启动后窗口会显示真实端口",
    )
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
    app = MonitorApp(root, args.host, args.port)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    bring_to_front(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
