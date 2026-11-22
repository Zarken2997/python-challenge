"""Tkinter 服务器监控台：启动/停止服务，实时查看“终端输出”和在线客户端。

界面上半部分是管理操作（启动、停止、公告、踢出），
右侧的「终端监控」面板与命令行版打印的内容一致：谁加入、谁离开、
谁说了什么、谁被踢出——即把 ``logging`` 记录实时镜像到窗口里，
不需要另开终端看日志。
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .config import DEFAULT_PORT, ServerConfig
from .server import ChatServer

LOGGER = logging.getLogger("task1.server")

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


class ServerApp:
    """服务器监控台窗口。"""

    def __init__(self, root: tk.Tk, host: str, port: int) -> None:
        self.root = root
        self.server: ChatServer | None = None
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.handler = QueueLogHandler(self.log_queue)
        self.handler.setLevel(logging.INFO)

        logger = logging.getLogger("task1")
        logger.setLevel(logging.INFO)
        logger.addHandler(self.handler)

        self.host_var = tk.StringVar(value=host)
        self.port_var = tk.StringVar(value=str(port))
        self.status_var = tk.StringVar(value="未启动")
        self.auto_scroll = tk.BooleanVar(value=True)

        self._build_ui()
        self._refresh_users()
        self._poll_logs()

    # ---------------------------------------------------------------- 界面

    def _build_ui(self) -> None:
        self.root.title("任务1 · 聊天服务器监控台")
        self.root.geometry("1120x700")
        self.root.minsize(900, 540)

        toolbar = ttk.Frame(self.root, padding=(10, 8))
        toolbar.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(toolbar, text="监听地址").pack(side=tk.LEFT)
        ttk.Entry(toolbar, textvariable=self.host_var, width=14).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(toolbar, text="端口").pack(side=tk.LEFT)
        ttk.Entry(toolbar, textvariable=self.port_var, width=7).pack(side=tk.LEFT, padx=(4, 12))
        self.start_button = ttk.Button(toolbar, text="启动服务", command=self.start_server)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(toolbar, text="停止服务", command=self.stop_server, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(toolbar, textvariable=self.status_var, foreground="#0a7d32").pack(side=tk.LEFT, padx=(16, 0))

        # 先占住底部的状态栏，再让中间面板吃掉剩余空间，否则底部会被挤出窗口
        status_bar = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.count_var = tk.StringVar(value="在线 0 人")
        ttk.Label(status_bar, textvariable=self.count_var).pack(side=tk.LEFT)
        ttk.Label(status_bar, text="提示：双击客户端行可直接踢出", foreground="#777").pack(side=tk.RIGHT)

        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        left = ttk.Labelframe(paned, text="在线客户端", padding=8)
        paned.add(left, weight=1)

        columns = ("addr", "duration")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings", height=14)
        self.tree.heading("#0", text="昵称")
        self.tree.heading("addr", text="地址")
        self.tree.heading("duration", text="时长")
        self.tree.column("#0", width=110, anchor=tk.W)
        self.tree.column("addr", width=160, anchor=tk.W)
        self.tree.column("duration", width=90, anchor=tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<Double-1>", lambda _event: self.kick_selected())

        left_buttons = ttk.Frame(left)
        left_buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(left_buttons, text="刷新", command=self._refresh_users_now).pack(side=tk.LEFT)
        ttk.Button(left_buttons, text="踢出选中", command=self.kick_selected).pack(side=tk.LEFT, padx=6)
        ttk.Button(left_buttons, text="发公告…", command=self.broadcast).pack(side=tk.LEFT)

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

    # ---------------------------------------------------------------- 操作

    def start_server(self) -> None:
        if self.server is not None:
            return
        host = self.host_var.get().strip() or "127.0.0.1"
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("端口无效", "端口必须是 1–65535 之间的整数")
            return
        if not 1 <= port <= 65535:
            messagebox.showerror("端口无效", "端口必须是 1–65535 之间的整数")
            return
        server = ChatServer(ServerConfig(host=host, port=port))
        try:
            bound_host, bound_port = server.start()
        except OSError as exc:
            self._append(f"! 启动失败: {exc}", "error")
            messagebox.showerror("启动失败", f"无法监听 {host}:{port}\n{exc}")
            return
        self.server = server
        self.status_var.set(f"监听 {bound_host}:{bound_port}")
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self._append(f"* 聊天服务器已启动 → {bound_host}:{bound_port}", "notice")

    def stop_server(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            self._append("* 服务器已停止，所有客户端连接已关闭", "notice")
        self.status_var.set("未启动")
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self._refresh_users_now()

    def broadcast(self) -> None:
        if self.server is None:
            messagebox.showinfo("未启动", "请先启动服务")
            return
        text = simpledialog.askstring("系统公告", "发送给所有在线用户：", parent=self.root)
        if not text or not text.strip():
            return
        delivered = self.server.broadcast_system(text)
        self._append(f"* 公告已发送给 {delivered} 人：{text.strip()}", "notice")

    def kick_selected(self) -> None:
        if self.server is None:
            messagebox.showinfo("未启动", "请先启动服务")
            return
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("未选择", "请先在左侧选择一个客户端")
            return
        name = selection[0]
        if not messagebox.askyesno("确认踢出", f"确定把 {name} 请出聊天室吗？"):
            return
        if not self.server.kick(name):
            self._append(f"! 踢出失败：{name} 已不在线", "warning")
        self._refresh_users_now()

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
            initialfile=time.strftime("chat-server-%Y%m%d-%H%M%S.log"),
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

    def on_close(self) -> None:
        self.stop_server()
        logging.getLogger("task1").removeHandler(self.handler)
        self.root.destroy()

    # ---------------------------------------------------------------- 刷新

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

    def _refresh_users_now(self) -> None:
        self._refresh_users()

    def _refresh_users(self) -> None:
        server = self.server
        existing = set(self.tree.get_children())
        rows: dict[str, tuple[str, str]] = {}
        if server is not None:
            for user in server.online_users():
                rows[str(user["name"])] = (
                    str(user["addr"]),
                    format_duration(int(user["connected_seconds"])),
                )
        for name in existing - set(rows):
            self.tree.delete(name)
        for name, (addr, duration) in rows.items():
            if name in existing:
                self.tree.item(name, values=(addr, duration))
            else:
                self.tree.insert("", tk.END, iid=name, text=name, values=(addr, duration))
        count = len(rows)
        self.count_var.set(f"在线 {count} 人")
        self.root.after(1000, self._refresh_users)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="任务1 聊天服务器图形监控台")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，局域网演示用 0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口（默认 9000）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = tk.Tk()
    app = ServerApp(root, args.host, args.port)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
