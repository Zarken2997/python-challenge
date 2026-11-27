"""服务器监控台里的「数据库审计」标签页：任务2 的图形界面。

设计取舍：
- 只用一个 Treeview，切换视图（运行日志 / 操作审计 / 聊天记录 / 事故记录），
  而不是并排四个大表格，屏幕再小也能用；
- 「浏览、排序、翻页、按条件查询」对所有视图通用；「增删改」只开放给
  人工维护的事故记录表，机器产生的日志和聊天记录保持只追加，符合审计要求；
- 数据库连接由界面自己持有（SQLite WAL 允许读写并发），写入仍由
  :class:`task2_db_system.recorder.AuditRecorder` 的单写线程负责。
"""

from __future__ import annotations

import csv
import sqlite3
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List

from ..task2_db_system.config import (
    ACTION_LABELS,
    DEFAULT_DB_PATH,
    MESSAGE_KINDS,
    PAGE_SIZES,
    SEVERITIES,
    STATUSES,
)
from ..task2_db_system.db import connect
from ..task2_db_system.repository import Repository
from ..task2_db_system.stats import overview

VIEWS = ("运行日志", "操作审计", "聊天记录", "事故记录")

# 每个视图：列（键, 标题, 宽度）、筛选下拉、排序字段集合。
VIEW_CONFIG: Dict[str, Dict[str, Any]] = {
    "运行日志": {
        "columns": (
            ("ts", "时间", 140),
            ("level", "级别", 70),
            ("event_type", "事件", 80),
            ("room_name", "房间", 90),
            ("actor", "用户", 90),
            ("message", "内容", 420),
        ),
        "filters": [("全部级别", "")] + [(level, level) for level in
                                        ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")],
        "sortable": ("ts", "level", "event_type", "room_name", "actor"),
    },
    "操作审计": {
        "columns": (
            ("ts", "时间", 140),
            ("action", "操作", 110),
            ("actor", "操作者", 90),
            ("target", "对象", 110),
            ("room_name", "房间", 90),
            ("result", "结果", 60),
            ("detail", "说明", 340),
        ),
        "filters": [("全部操作", "")] + [(label, key) for key, label in ACTION_LABELS.items()],
        "sortable": ("ts", "action", "actor", "room_name", "result"),
    },
    "聊天记录": {
        "columns": (
            ("ts", "时间", 140),
            ("kind", "类型", 60),
            ("room_name", "房间", 90),
            ("sender", "发送者", 90),
            ("recipient", "接收者", 90),
            ("content", "内容", 400),
        ),
        "filters": [("全部类型", "")] + [(kind, kind) for kind in MESSAGE_KINDS],
        "sortable": ("ts", "kind", "sender", "room_name", "content_len"),
    },
    "事故记录": {
        "columns": (
            ("id", "编号", 50),
            ("title", "标题", 200),
            ("severity", "级别", 60),
            ("status", "状态", 80),
            ("assignee", "负责人", 90),
            ("updated_at", "更新时间", 140),
        ),
        "filters": [("全部状态", "")] + [(status, status) for status in STATUSES],
        "sortable": ("id", "created_at", "updated_at", "severity", "status", "title"),
    },
}


def _short(value: object, limit: int = 90) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AuditPanel(ttk.Frame):
    """任务2 的查询与维护界面。"""

    def __init__(self, master: tk.Misc, db_path: Path | None = None) -> None:
        super().__init__(master, padding=6)
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.conn: sqlite3.Connection | None = None
        self.repo: Repository | None = None
        self.view_var = tk.StringVar(value=VIEWS[0])
        self.keyword_var = tk.StringVar()
        self.filter_var = tk.StringVar()
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.page = 1
        self.page_count = 1
        self.total = 0
        self.sort_key = "ts"
        self.sort_desc = True
        self.page_label_var = tk.StringVar(value="第 1 / 1 页 · 共 0 条")
        self.summary_var = tk.StringVar(value="数据库：未连接")
        self._rows: List[Dict[str, Any]] = []
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------ 界面

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="视图").pack(side=tk.LEFT)
        view_box = ttk.Combobox(
            top, textvariable=self.view_var, values=VIEWS, state="readonly", width=10
        )
        view_box.pack(side=tk.LEFT, padx=(4, 10))
        view_box.bind("<<ComboboxSelected>>", lambda _event: self.switch_view())

        ttk.Label(top, text="关键字").pack(side=tk.LEFT)
        keyword_entry = ttk.Entry(top, textvariable=self.keyword_var, width=18)
        keyword_entry.pack(side=tk.LEFT, padx=(4, 8))
        keyword_entry.bind("<Return>", lambda _event: self.search())

        self.filter_box = ttk.Combobox(top, textvariable=self.filter_var, state="readonly", width=12)
        self.filter_box.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(top, text="从").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.start_var, width=11).pack(side=tk.LEFT, padx=(2, 4))
        ttk.Label(top, text="到").pack(side=tk.LEFT)
        ttk.Entry(top, textvariable=self.end_var, width=11).pack(side=tk.LEFT, padx=(2, 6))
        ttk.Button(top, text="查询", command=self.search).pack(side=tk.LEFT)
        ttk.Button(top, text="重置", command=self.reset).pack(side=tk.LEFT, padx=(4, 0))

        summary = ttk.Label(self, textvariable=self.summary_var, foreground="#0b5fa5")
        summary.grid(row=1, column=0, sticky="w", pady=(4, 2))

        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, sticky="ew", pady=(2, 4))
        ttk.Button(bar, text="上一页", command=self.prev_page).pack(side=tk.LEFT)
        ttk.Button(bar, text="下一页", command=self.next_page).pack(side=tk.LEFT, padx=4)
        ttk.Label(bar, textvariable=self.page_label_var).pack(side=tk.LEFT, padx=6)
        ttk.Label(bar, text="每页").pack(side=tk.LEFT, padx=(8, 2))
        self.page_size_box = ttk.Combobox(
            bar, values=[str(size) for size in PAGE_SIZES], width=5, state="readonly"
        )
        self.page_size_box.set("50")
        self.page_size_box.pack(side=tk.LEFT)
        self.page_size_box.bind("<<ComboboxSelected>>", lambda _event: self._change_page_size())
        ttk.Button(bar, text="导出 CSV…", command=self.export_csv).pack(side=tk.RIGHT)
        self.incident_buttons = ttk.Frame(bar)
        self.incident_buttons.pack(side=tk.RIGHT, padx=8)
        ttk.Button(self.incident_buttons, text="新增事故", command=self.add_incident).pack(side=tk.LEFT)
        ttk.Button(self.incident_buttons, text="编辑", command=self.edit_incident).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Button(self.incident_buttons, text="删除", command=self.delete_incident).pack(side=tk.LEFT)

        table_frame = ttk.Frame(self)
        table_frame.grid(row=3, column=0, sticky="nsew")
        y_scroll = ttk.Scrollbar(table_frame)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        x_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL)
        x_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree = ttk.Treeview(table_frame, show="headings", yscrollcommand=y_scroll.set,
                                 xscrollcommand=x_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y_scroll.config(command=self.tree.yview)
        x_scroll.config(command=self.tree.xview)
        self.tree.bind("<Double-1>", lambda _event: self.show_detail())

        hint = ttk.Label(
            self,
            text="提示：点击表头排序；双击行查看完整内容。日志与聊天记录只读，事故记录可增删改。",
            foreground="#777",
        )
        hint.grid(row=4, column=0, sticky="w", pady=(4, 0))

        self._apply_view_config()

    def _apply_view_config(self) -> None:
        config = VIEW_CONFIG[self.view_var.get()]
        self.filter_box.configure(values=[label for label, _value in config["filters"]])
        self.filter_var.set(config["filters"][0][0])
        self.tree.delete(*self.tree.get_children())
        self.tree.configure(columns=[key for key, _title, _width in config["columns"]])
        for key, title, width in config["columns"]:
            self.tree.heading(key, text=title, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor=tk.W, stretch=(key in ("message", "content", "detail")))
        is_incident = self.view_var.get() == "事故记录"
        for child in self.incident_buttons.winfo_children():
            child.configure(state=tk.NORMAL if is_incident else tk.DISABLED)
        self._view_applied = self.view_var.get()

    # ------------------------------------------------------------ 数据

    def _ensure_conn(self) -> bool:
        if self.conn is not None:
            return True
        if not Path(self.db_path).exists():
            self.summary_var.set(f"数据库尚未创建：启动注册中心后自动建库（{self.db_path}）")
            return False
        try:
            self.conn = connect(self.db_path)
            self.repo = Repository(self.conn)
        except sqlite3.Error as exc:
            self.summary_var.set(f"打开数据库失败：{exc}")
            return False
        return True

    def _selected_filter(self) -> str:
        label = self.filter_var.get()
        for text, value in VIEW_CONFIG[self.view_var.get()]["filters"]:
            if text == label:
                return value
        return ""

    def refresh(self) -> None:
        """按当前视图/筛选/分页重新查询并刷新表格。"""
        if not self._ensure_conn() or self.repo is None:
            return
        view = self.view_var.get()
        if getattr(self, "_view_applied", view) != view:
            # 视图变量被程序化修改时不会触发下拉框事件，这里补一次列/筛选重建。
            self._apply_view_config()
        config = VIEW_CONFIG.get(view, VIEW_CONFIG[VIEWS[0]])
        if self.sort_key not in config["sortable"]:
            # 程序化切换视图不会触发下拉框事件，排序键可能还是上一个视图的；
            # 这里兜底回退到该视图的默认排序，避免弹出"不支持按 ts 排序"。
            self.sort_key = "updated_at" if view == "事故记录" else "ts"
            self.sort_desc = True
        keyword = self.keyword_var.get().strip()
        value = self._selected_filter()
        start = self.start_var.get().strip()
        end = self.end_var.get().strip()
        try:
            page_size = int(self.page_size_box.get())
        except ValueError:
            page_size = 50
        try:
            if view == "运行日志":
                result = self.repo.list_logs(
                    keyword=keyword, level=value, start=start, end=end,
                    sort=self.sort_key, desc=self.sort_desc, page=self.page, page_size=page_size,
                )
                rows = result["rows"]
                values = [
                    (row["ts"], row["level"], row["event_type"], row["room_name"],
                     row["actor"], _short(row["message"]))
                    for row in rows
                ]
            elif view == "操作审计":
                result = self.repo.list_operations(
                    keyword=keyword, action=value, start=start, end=end,
                    sort=self.sort_key, desc=self.sort_desc, page=self.page, page_size=page_size,
                )
                rows = result["rows"]
                values = [
                    (row["ts"], ACTION_LABELS.get(row["action"], row["action"]), row["actor"],
                     row["target"], row["room_name"], row["result"], _short(row["detail"]))
                    for row in rows
                ]
            elif view == "聊天记录":
                result = self.repo.list_messages(
                    keyword=keyword, kind=value, start=start, end=end,
                    sort=self.sort_key, desc=self.sort_desc, page=self.page, page_size=page_size,
                )
                rows = result["rows"]
                values = [
                    (row["ts"], row["kind"], row["room_name"], row["sender"], row["recipient"],
                     _short(row["content"]))
                    for row in rows
                ]
            else:
                result = self.repo.list_incidents(
                    keyword=keyword, status=value,
                    sort=self.sort_key, desc=self.sort_desc, page=self.page, page_size=page_size,
                )
                rows = result["rows"]
                values = [
                    (row["id"], row["title"], row["severity"], row["status"], row["assignee"],
                     row["updated_at"])
                    for row in rows
                ]
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror("查询失败", str(exc), parent=self)
            return
        self._rows = rows
        self.total = int(result["total"])
        self.page_count = int(result.get("pages", 1))
        self.tree.delete(*self.tree.get_children())
        for row, row_values in zip(rows, values):
            self.tree.insert("", tk.END, iid=str(row["id"]), values=row_values)
        self.page_label_var.set(f"第 {self.page} / {self.page_count} 页 · 共 {self.total} 条")
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        if self.conn is None:
            return
        try:
            info = overview(self.conn)
        except sqlite3.Error:
            return
        self.summary_var.set(
            f"日志 {info['logs_total']} 条（错误 {info['logs_error']}，警告 {info['logs_warning']}）"
            f" · 聊天 {info['messages_total']} 条 · 操作 {info['operations_total']} 条"
            f" · 未关闭事故 {info['incidents_open']} 个 · 在线会话 {info['sessions_active']} 个"
        )

    # ------------------------------------------------------------ 操作

    def switch_view(self) -> None:
        view = self.view_var.get()
        self.sort_key = "updated_at" if view == "事故记录" else "ts"
        self.sort_desc = True
        self.page = 1
        self.keyword_var.set("")
        self.start_var.set("")
        self.end_var.set("")
        self._apply_view_config()
        self.refresh()

    def search(self) -> None:
        self.page = 1
        self.refresh()

    def reset(self) -> None:
        self.keyword_var.set("")
        self.start_var.set("")
        self.end_var.set("")
        self.filter_var.set(VIEW_CONFIG[self.view_var.get()]["filters"][0][0])
        self.page = 1
        self.refresh()

    def sort_by(self, key: str) -> None:
        if key not in VIEW_CONFIG[self.view_var.get()]["sortable"]:
            return
        if self.sort_key == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key = key
            self.sort_desc = True
        self.page = 1
        self.refresh()

    def prev_page(self) -> None:
        if self.page > 1:
            self.page -= 1
            self.refresh()

    def next_page(self) -> None:
        if self.page < self.page_count:
            self.page += 1
            self.refresh()

    def _change_page_size(self) -> None:
        self.page = 1
        self.refresh()

    def show_detail(self) -> None:
        selection = self.tree.selection()
        if not selection or not self._rows:
            return
        row = None
        for candidate in self._rows:
            if str(candidate["id"]) == selection[0]:
                row = candidate
                break
        if row is None:
            return
        window = tk.Toplevel(self)
        window.title(f"{self.view_var.get()} · 第 {row['id']} 条")
        window.geometry("640x420")
        text = tk.Text(window, wrap=tk.WORD, font=("Consolas", 10))
        text.pack(fill=tk.BOTH, expand=True)
        for key, value in row.items():
            text.insert(tk.END, f"{key}:\n{value}\n\n")
        text.config(state=tk.DISABLED)
        ttk.Button(window, text="关闭", command=window.destroy).pack(pady=6)

    # ------------------------------------------------------------ 事故记录维护

    def _selected_incident(self) -> Dict[str, Any] | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("未选择", "请先在事故列表中选择一行", parent=self)
            return None
        for row in self._rows:
            if str(row["id"]) == selection[0]:
                return row
        return None

    def add_incident(self) -> None:
        if self.repo is None:
            return
        values = IncidentDialog(self, None).show()
        if values is None:
            return
        try:
            new_id = self.repo.create_incident(**values)
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror("新增失败", str(exc), parent=self)
            return
        self.repo.insert_operation(
            "INCIDENT_CREATE", actor="管理员", target=str(new_id),
            detail=values.get("title", ""),
        )
        self.refresh()
        messagebox.showinfo("已新增", f"事故记录 #{new_id} 已创建", parent=self)

    def edit_incident(self) -> None:
        row = self._selected_incident()
        if row is None or self.repo is None:
            return
        values = IncidentDialog(self, row).show()
        if values is None:
            return
        try:
            updated = self.repo.update_incident(int(row["id"]), **values)
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror("修改失败", str(exc), parent=self)
            return
        if updated:
            self.repo.insert_operation(
                "INCIDENT_UPDATE", actor="管理员", target=str(row["id"]),
                detail=values.get("title", ""),
            )
        self.refresh()

    def delete_incident(self) -> None:
        row = self._selected_incident()
        if row is None or self.repo is None:
            return
        if not messagebox.askyesno(
            "确认删除", f"确定删除事故记录 #{row['id']}「{row['title']}」吗？此操作不可撤销。",
            parent=self,
        ):
            return
        try:
            deleted = self.repo.delete_incident(int(row["id"]))
        except sqlite3.Error as exc:
            messagebox.showerror("删除失败", str(exc), parent=self)
            return
        if deleted:
            self.repo.insert_operation(
                "INCIDENT_DELETE", actor="管理员", target=str(row["id"]), detail=str(row["title"])
            )
        self.refresh()

    # ------------------------------------------------------------ 导出

    def export_csv(self) -> None:
        if self.repo is None or not self._rows:
            messagebox.showinfo("没有数据", "当前视图没有可导出的记录", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="导出当前查询结果",
            defaultextension=".csv",
            initialfile=f"audit-{self.view_var.get()}.csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(self._rows[0].keys()))
                writer.writeheader()
                writer.writerows(self._rows)
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        messagebox.showinfo("已导出", f"当前页 {len(self._rows)} 条已写入\n{path}", parent=self)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None


class IncidentDialog(tk.Toplevel):
    """新增/编辑事故记录的小窗口。"""

    def __init__(self, master: tk.Misc, row: Dict[str, Any] | None = None) -> None:
        super().__init__(master)
        self.title("新增事故记录" if row is None else f"编辑事故记录 #{row['id']}")
        self.resizable(False, False)
        self.result: Dict[str, Any] | None = None
        self.transient(master.winfo_toplevel())
        self.grab_set()

        self.title_var = tk.StringVar(value=str(row["title"]) if row else "")
        self.assignee_var = tk.StringVar(value=str(row["assignee"]) if row else "")
        self.severity_var = tk.StringVar(value=str(row["severity"]) if row else "中")
        self.status_var = tk.StringVar(value=str(row["status"]) if row else "待处理")
        self.log_var = tk.StringVar(value=str(row["log_event_id"] or "") if row else "")

        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="标题 *").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(body, textvariable=self.title_var, width=46).grid(row=0, column=1, columnspan=3, sticky="ew", pady=3)
        ttk.Label(body, text="严重级别").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(body, textvariable=self.severity_var, values=list(SEVERITIES),
                     state="readonly", width=10).grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(body, text="状态").grid(row=1, column=2, sticky="e", padx=(8, 4))
        ttk.Combobox(body, textvariable=self.status_var, values=list(STATUSES),
                     state="readonly", width=10).grid(row=1, column=3, sticky="w", pady=3)
        ttk.Label(body, text="负责人").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(body, textvariable=self.assignee_var, width=20).grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(body, text="关联日志 ID").grid(row=2, column=2, sticky="e", padx=(8, 4))
        ttk.Entry(body, textvariable=self.log_var, width=12).grid(row=2, column=3, sticky="w", pady=3)
        ttk.Label(body, text="描述").grid(row=3, column=0, sticky="nw", pady=3)
        self.description = tk.Text(body, width=52, height=6, wrap=tk.WORD)
        self.description.grid(row=3, column=1, columnspan=3, sticky="ew", pady=3)
        if row:
            self.description.insert("1.0", str(row["description"]))
        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=4, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="保存", command=self._on_save).pack(side=tk.LEFT)
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side=tk.LEFT, padx=6)

    def _on_save(self) -> None:
        title = self.title_var.get().strip()
        if not title:
            messagebox.showerror("标题为空", "事故标题不能为空", parent=self)
            return
        log_text = self.log_var.get().strip()
        try:
            log_id = int(log_text) if log_text else None
        except ValueError:
            messagebox.showerror("格式错误", "关联日志 ID 必须是整数", parent=self)
            return
        self.result = {
            "title": title,
            "severity": self.severity_var.get(),
            "status": self.status_var.get(),
            "assignee": self.assignee_var.get().strip(),
            "description": self.description.get("1.0", tk.END).strip(),
            "log_event_id": log_id,
        }
        self.destroy()

    def show(self) -> Dict[str, Any] | None:
        self.wait_window()
        return self.result


def build_audit_tab(master: tk.Misc, db_path: Path | None = None) -> AuditPanel:
    """给监控台创建审计标签页（供 gui_server 调用）。"""
    return AuditPanel(master, db_path)
