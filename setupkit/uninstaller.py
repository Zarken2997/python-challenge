"""卸载器：结束进程、删快捷方式、删数据目录、删安装目录、删注册表项，最后自删。

「卸载干净」的标准写在 `cleanup.removal_plan()` 里：快捷方式 + %APPDATA% 数据 + 安装目录 +
HKCU 卸载项，一样不留。卸载器自己就在安装目录里，所以最后一步要交给一个延迟执行的
批处理去删目录和它自己（Windows 不允许删除正在运行的可执行文件）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):  # 允许直接 `python setupkit\\uninstaller.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "setupkit"

from . import app_meta, cleanup, diag, registry_ops, shortcuts  # noqa: E402

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def running_exe() -> Path:
    """当前进程对应的可执行文件（打包后是 Uninstall.exe）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def kill_running_client(log=print) -> None:
    """结束正在运行的客户端，避免它占用文件导致删不掉。"""
    for name in cleanup.app_process_names():
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True,
                timeout=20,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            log("* 未能结束进程 {}（可能本来就没运行）".format(name))
    time.sleep(0.4)


def remove_tree(path: Path, log=print, attempts: int = 3) -> bool:
    """递归删除目录，遇到占用/只读重试几次；返回是否删干净。"""
    if not path.exists():
        return True
    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            log("* 第 {} 次删除 {} 失败: {}".format(attempt, path, exc))
            _clear_readonly(path)
            time.sleep(0.6)
    return not path.exists()


def _clear_readonly(path: Path) -> None:
    for child in path.rglob("*"):
        try:
            os.chmod(child, 0o700)
        except OSError:
            continue


def remove_shortcuts(install_root: Path, log=print) -> list[Path]:
    """删除安装器创建过的快捷方式，返回没有删掉的路径。"""
    failures = []
    for link in cleanup.shortcut_files(
        install_root, app_meta.desktop_dir(), app_meta.start_menu_dir()
    ):
        if not shortcuts.remove_shortcut(link):
            failures.append(link)
    shortcuts.remove_empty_dir(app_meta.start_menu_dir() / app_meta.START_MENU_FOLDER)
    return failures


def self_delete_script(install_root: Path) -> str:
    """延迟批处理的正文：等卸载器退出 → 删安装目录 → 删批处理自己。"""
    return (
        "@echo off\r\n"
        "timeout /t 3 /nobreak > nul\r\n"
        'rmdir /s /q "{0}" >> "%TEMP%\\python-chat-client-uninstall.log" 2>&1\r\n'
        'del "%~f0"\r\n'
    ).format(install_root)


def schedule_self_delete(install_root: Path) -> Path:
    """把自删批处理写到临时目录并以脱离方式启动。"""
    path = Path(tempfile.gettempdir()) / "python-chat-client-uninstall-{}.cmd".format(os.getpid())
    with open(str(path), "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(self_delete_script(install_root))
    subprocess.Popen(
        ["cmd", "/c", str(path)],
        creationflags=_DETACHED,
        close_fds=True,
        cwd=str(Path(tempfile.gettempdir())),
    )
    return path


def leftovers(install_root: Path, data_root: Path, desktop: Path, start_menu: Path) -> list[str]:
    """自检：列出仍然存在的痕迹（空列表 = 卸载干净）。"""
    remaining = []
    for path in cleanup.removal_plan(install_root, data_root, desktop, start_menu):
        if path.exists():
            remaining.append(str(path))
    if registry_ops.read_values():
        remaining.append("HKCU\\{}".format(app_meta.UNINSTALL_KEY))
    return remaining


def uninstall(
    install_root: Path,
    keep_data: bool = False,
    log=print,
    inside_running_dir: bool | None = None,
) -> int:
    """执行卸载；返回 0 表示成功（含「已安排自删」），1 表示仍有残留。"""
    data_root = app_meta.data_dir()
    desktop = app_meta.desktop_dir()
    start_menu = app_meta.start_menu_dir()

    if not cleanup.is_safe_install_root(install_root):
        log("! 安装目录不安全，已拒绝卸载: {}".format(install_root))
        return 1

    if not cleanup.looks_like_install_dir(install_root):
        # 这是本代理误删事件的直接教训：没有安装标记的目录一律不删。
        log("! 目标目录不像本程序的安装目录（缺少 {} 或两个 exe），已拒绝卸载: {}".format(
            app_meta.INSTALL_MARKER, install_root
        ))
        log("  如确认要卸载，请用 --dir 指向真正的安装目录（{}）。".format(app_meta.install_dir()))
        return 1

    kill_running_client(log)

    for link in remove_shortcuts(install_root, log):
        log("! 快捷方式没删掉: {}".format(link))

    if keep_data:
        log("* 保留用户数据目录（按 --keep-data）: {}".format(data_root))
    elif remove_tree(data_root, log):
        log("* 已删除用户数据目录: {}".format(data_root))
    else:
        log("! 用户数据目录删除不完整: {}".format(data_root))

    if inside_running_dir is None:
        exe_dir = running_exe().parent
        inside_running_dir = cleanup.is_inside(exe_dir, install_root)

    if inside_running_dir:
        # 卸载器自己在安装目录里：先删掉除自身之外的其它东西，再交给批处理收尾
        current = running_exe()
        for child in install_root.iterdir():
            try:
                is_self = child.resolve() == current
            except OSError:
                is_self = False
            if is_self:
                continue
            if child.is_dir():
                remove_tree(child, log)
            else:
                try:
                    child.unlink()
                except OSError as exc:
                    log("! 删除失败 {}: {}".format(child, exc))
        registry_ops.delete_uninstall_key()
        schedule_self_delete(install_root)
        log("* 已安排删除安装目录: {}（本窗口关闭后约 3 秒完成）".format(install_root))
        return 0

    if remove_tree(install_root, log):
        log("* 已删除安装目录: {}".format(install_root))
    else:
        log("! 安装目录删除不完整: {}".format(install_root))

    registry_ops.delete_uninstall_key()
    remaining = leftovers(install_root, data_root, desktop, start_menu)
    if remaining:
        for item in remaining:
            log("! 仍有残留: {}".format(item))
        return 1
    log("* 卸载完成，未发现残留")
    return 0


# ------------------------------------------------------------------ 图形向导


def run_gui(install_root: Path, keep_data: bool) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("卸载 {}".format(app_meta.DISPLAY_NAME))
    root.geometry("580x280+220+180")
    root.resizable(False, False)

    ttk.Label(
        root, text="卸载 {}".format(app_meta.DISPLAY_NAME), font=("Microsoft YaHei", 14, "bold")
    ).pack(anchor=tk.W, padx=18, pady=(16, 4))
    ttk.Label(root, text="安装目录：{}".format(install_root), wraplength=540).pack(anchor=tk.W, padx=18)
    ttk.Label(
        root,
        text="将删除：程序文件、桌面与开始菜单快捷方式、用户配置与日志，以及「设置 → 应用」里的卸载项。",
        wraplength=540,
        foreground="#8a4b08",
    ).pack(anchor=tk.W, padx=18, pady=(8, 0))

    keep_var = tk.BooleanVar(value=keep_data)
    ttk.Checkbutton(root, text="保留 %APPDATA% 下的用户配置", variable=keep_var).pack(
        anchor=tk.W, padx=18, pady=(8, 0)
    )

    status = tk.StringVar(value="")
    ttk.Label(root, textvariable=status, foreground="#0b5fa5").pack(anchor=tk.W, padx=18, pady=(8, 0))

    buttons = ttk.Frame(root)
    buttons.pack(fill=tk.X, padx=18, pady=14)
    confirm = ttk.Button(buttons, text="确认卸载")

    def do_uninstall() -> None:
        if not messagebox.askyesno("确认卸载", "确定要卸载 {} 吗？".format(app_meta.DISPLAY_NAME)):
            return
        confirm.config(state=tk.DISABLED)
        status.set("正在卸载…")
        root.update_idletasks()
        code = uninstall(install_root, keep_data=keep_var.get())
        if code == 0:
            messagebox.showinfo("卸载完成", "已卸载并清理干净。")
            root.destroy()
        else:
            status.set("卸载未完全成功")
            confirm.config(state=tk.NORMAL)
            messagebox.showwarning("卸载未完成", "仍有文件残留，请查看提示后重试或手动删除。")

    confirm.config(command=do_uninstall)
    confirm.pack(side=tk.RIGHT)
    ttk.Button(buttons, text="取消", command=root.destroy).pack(side=tk.RIGHT, padx=8)
    root.mainloop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="卸载 {}".format(app_meta.DISPLAY_NAME))
    parser.add_argument("--silent", action="store_true", help="不显示界面，直接卸载")
    parser.add_argument("--dir", default="", help="安装目录（默认从注册表读取）")
    parser.add_argument("--keep-data", action="store_true", help="保留 %%APPDATA%% 下的用户配置")
    return parser


def resolve_install_root(explicit: str) -> Path | None:
    """按「命令行 > 注册表 > 自身所在目录」判断安装目录，且只接受像安装目录的路径。

    关键约束：绝不把「卸载器碰巧所在的目录」直接当成安装目录——曾经因此在
    `F:\\codex,ultilize` 下双击运行，把整棵项目目录递归删掉。
    """
    if explicit.strip():
        return Path(explicit)
    from_registry = registry_ops.install_location()
    if from_registry is not None and cleanup.looks_like_install_dir(from_registry):
        return from_registry
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if cleanup.looks_like_install_dir(exe_dir):
            return exe_dir
    return None


def show_error(title: str, message: str) -> None:
    """GUI 程序没有控制台，出错时尽量弹窗告知。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:  # noqa: BLE001 - 弹窗失败不应掩盖原始错误
        return


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    install_root = resolve_install_root(args.dir)
    if install_root is None:
        print("! 找不到安装目录，请用 --dir 指定")
        return 1
    diag.log("卸载开始: dir={} silent={} keep_data={}".format(install_root, args.silent, args.keep_data))
    try:
        if args.silent:
            code = uninstall(install_root, keep_data=args.keep_data)
        else:
            code = run_gui(install_root, keep_data=args.keep_data)
    except Exception:  # noqa: BLE001 - 顶层兜底，保证日志里有完整堆栈
        diag.log_exception("卸载")
        show_error("卸载失败", "卸载过程中出现异常，详情见日志：\n{}".format(diag.log_path()))
        return 1
    diag.log("卸载结束: code={}".format(code))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
