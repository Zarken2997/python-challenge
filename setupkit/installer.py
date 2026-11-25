"""安装器：把打包好的 ChatClient.exe / Uninstall.exe 装到当前用户目录并登记卸载项。

设计目标：

* 免管理员（整个安装只写 HKCU、%LOCALAPPDATA%、%APPDATA% 和快捷方式目录）；
* 有图形向导，也能 `--silent` 静默安装（自动化测试与批量部署用）；
* 只做「复制文件 + 建快捷方式 + 写卸载键」三件事，卸载逻辑全部放在 Uninstall.exe 里。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # 允许直接 `python setupkit\\installer.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "setupkit"

from . import app_meta, cleanup, diag, registry_ops, shortcuts  # noqa: E402

PAYLOAD_FILES = (app_meta.CLIENT_EXE, app_meta.UNINSTALL_EXE)


def payload_dir() -> Path:
    """打包后文件在 PyInstaller 解包目录的 app 子目录；源码运行时取仓库 dist/app。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "app"
    return Path(__file__).resolve().parents[1] / "dist" / "app"


def copy_payload(install_root: Path) -> list[Path]:
    """复制可执行文件，返回实际写入的路径；缺文件抛 FileNotFoundError。"""
    source = payload_dir()
    missing = [name for name in PAYLOAD_FILES if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError("安装包缺少文件: {}".format("、".join(missing)))
    install_root.mkdir(parents=True, exist_ok=True)
    written = []
    for name in PAYLOAD_FILES:
        target = install_root / name
        shutil.copy2(source / name, target)
        written.append(target)
    return written


def make_shortcuts(install_root: Path, desktop: bool = True, start_menu: bool = True) -> list[str]:
    """按选项创建快捷方式，返回生成失败的说明（空列表表示全部成功）。"""
    problems = []
    exe = app_meta.exe_path(install_root)
    uninstaller = app_meta.uninstaller_path(install_root)
    if desktop:
        ok, detail = shortcuts.create_shortcut(
            app_meta.desktop_dir() / app_meta.client_shortcut_name(),
            exe,
            description="连接聊天室注册中心并聊天",
        )
        if not ok:
            problems.append("桌面快捷方式: {}".format(detail))
    if start_menu:
        folder = app_meta.start_menu_dir() / app_meta.START_MENU_FOLDER
        ok, detail = shortcuts.create_shortcut(
            folder / app_meta.client_shortcut_name(), exe, description="连接聊天室注册中心并聊天"
        )
        if not ok:
            problems.append("开始菜单快捷方式: {}".format(detail))
        ok, detail = shortcuts.create_shortcut(
            folder / app_meta.uninstall_shortcut_name(),
            uninstaller,
            description="卸载 {}".format(app_meta.DISPLAY_NAME),
        )
        if not ok:
            problems.append("卸载快捷方式: {}".format(detail))
    return problems


def write_marker(install_root: Path) -> Path:
    """在安装目录写入标记文件，卸载器只认带这个标记的目录。"""
    path = app_meta.marker_path(install_root)
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("{} {}\n".format(app_meta.APP_ID, app_meta.VERSION))
        handle.write("installed_at={}\n".format(time.strftime("%Y-%m-%d %H:%M:%S")))
    return path


def estimated_size_kb(install_root: Path) -> int:
    total = 0
    for path in install_root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return max(1, total // 1024)


def install(
    install_root: Path,
    desktop: bool = True,
    start_menu: bool = True,
    log=print,
) -> int:
    """执行安装，返回 0 成功 / 1 失败。"""
    if not cleanup.is_safe_install_root(install_root):
        log("! 安装目录不安全，已拒绝: {}".format(install_root))
        return 1
    if install_root.exists() and not cleanup.looks_like_install_dir(install_root):
        # 往别人的目录里装会把标记写进去，卸载时就会连那个目录一起删——宁可拒绝。
        try:
            occupied = any(install_root.iterdir())
        except OSError as exc:
            log("! 无法读取安装目录: {}".format(exc))
            return 1
        if occupied:
            log("! 安装目录不是空的，也不是本程序的旧安装目录，已拒绝: {}".format(install_root))
            log("  请换一个空目录，或使用默认目录: {}".format(app_meta.install_dir()))
            return 1
    try:
        written = copy_payload(install_root)
    except (OSError, FileNotFoundError) as exc:
        log("! 复制文件失败: {}".format(exc))
        diag.log("复制文件失败: {!r}".format(exc))
        return 1
    log("* 已复制 {} 个文件到 {}".format(len(written), install_root))
    write_marker(install_root)

    problems = make_shortcuts(install_root, desktop=desktop, start_menu=start_menu)
    for problem in problems:
        log("! 快捷方式创建失败（不影响使用）: {}".format(problem))

    values = registry_ops.uninstall_values(
        install_root,
        estimated_kb=estimated_size_kb(install_root),
        install_date=time.strftime("%Y%m%d"),
    )
    try:
        registry_ops.write_uninstall_key(install_root, values)
    except OSError as exc:
        log("! 写入卸载注册表失败: {}".format(exc))
        return 1
    log("* 已登记卸载项: HKCU\\{}".format(app_meta.UNINSTALL_KEY))
    return 0


def open_client(target: Path) -> None:
    """安装完成后按用户意愿启动客户端。"""
    import subprocess

    exe = app_meta.exe_path(target)
    if not exe.is_file():
        return
    try:
        subprocess.Popen([str(exe)], cwd=str(target), close_fds=True)
    except OSError as exc:
        print("! 启动客户端失败: {}".format(exc))


# ------------------------------------------------------------------ 图形向导


def run_gui(default_dir: Path, run_after: bool = False) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("安装 {}".format(app_meta.DISPLAY_NAME))
    root.geometry("640x310+200+160")
    root.resizable(False, False)

    ttk.Label(
        root, text="安装 {}".format(app_meta.DISPLAY_NAME), font=("Microsoft YaHei", 14, "bold")
    ).pack(anchor=tk.W, padx=18, pady=(16, 4))
    ttk.Label(root, text="版本 {} · {}".format(app_meta.VERSION, app_meta.PUBLISHER)).pack(
        anchor=tk.W, padx=18
    )

    dir_var = tk.StringVar(value=str(default_dir))
    frame = ttk.Frame(root)
    frame.pack(fill=tk.X, padx=18, pady=(14, 4))
    ttk.Label(frame, text="安装到").pack(side=tk.LEFT)
    ttk.Entry(frame, textvariable=dir_var, width=58).pack(side=tk.LEFT, padx=8)
    ttk.Button(
        frame,
        text="浏览…",
        command=lambda: dir_var.set(
            filedialog.askdirectory(initialdir=str(default_dir)) or dir_var.get()
        ),
    ).pack(side=tk.LEFT)

    desktop_var = tk.BooleanVar(value=True)
    startmenu_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(root, text="创建桌面快捷方式", variable=desktop_var).pack(anchor=tk.W, padx=18)
    ttk.Checkbutton(root, text="创建开始菜单快捷方式（含卸载入口）", variable=startmenu_var).pack(
        anchor=tk.W, padx=18
    )

    status = tk.StringVar(value="点击「开始安装」")
    ttk.Label(root, textvariable=status, foreground="#0b5fa5").pack(anchor=tk.W, padx=18, pady=(10, 0))

    buttons = ttk.Frame(root)
    buttons.pack(fill=tk.X, padx=18, pady=12)
    install_button = ttk.Button(buttons, text="开始安装")
    install_button.pack(side=tk.RIGHT)
    ttk.Button(buttons, text="取消", command=root.destroy).pack(side=tk.RIGHT, padx=8)

    def do_install() -> None:
        target = Path(dir_var.get().strip() or str(default_dir))
        status.set("正在安装…")
        install_button.config(state=tk.DISABLED)
        root.update_idletasks()
        code = install(target, desktop=desktop_var.get(), start_menu=startmenu_var.get())
        if code != 0:
            status.set("安装失败，详见提示")
            install_button.config(state=tk.NORMAL)
            messagebox.showerror("安装失败", "安装未完成，请检查目录权限后重试。")
            return
        status.set("安装完成")
        if messagebox.askyesno("安装完成", "已安装到：\n{}\n\n现在启动客户端吗？".format(target)):
            open_client(target)
        root.destroy()

    install_button.config(command=do_install)
    if run_after:
        root.after(100, do_install)
    root.mainloop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安装 {}".format(app_meta.DISPLAY_NAME))
    parser.add_argument("--silent", action="store_true", help="不显示界面，直接安装")
    parser.add_argument("--dir", default=str(app_meta.install_dir()), help="安装目录")
    parser.add_argument("--no-desktop", action="store_true", help="不创建桌面快捷方式")
    parser.add_argument("--no-startmenu", action="store_true", help="不创建开始菜单快捷方式")
    parser.add_argument("--run", action="store_true", help="安装完成后启动客户端")
    return parser


def show_error(title: str, message: str) -> None:
    """GUI 程序没有控制台，出错时尽量弹窗告知；没有 tkinter 就静默。"""
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
    target = Path(args.dir)
    diag.log("安装开始: dir={} silent={} desktop={}".format(target, args.silent, not args.no_desktop))
    try:
        if args.silent:
            code = install(target, desktop=not args.no_desktop, start_menu=not args.no_startmenu)
        else:
            code = run_gui(target, run_after=args.run)
    except Exception:  # noqa: BLE001 - 顶层兜底，保证日志里有完整堆栈
        diag.log_exception("安装")
        show_error("安装失败", "安装过程中出现异常，详情见日志：\n{}".format(diag.log_path()))
        return 1
    diag.log("安装结束: code={}".format(code))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
