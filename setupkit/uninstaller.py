"""卸载器：按安装清单逐项核验删除，最后以「临时副本」方式删掉自己和安装目录。

三条主流做法都在这里落地：

1. **清单驱动**：只删安装器写在 `install-manifest.json` 里的文件、快捷方式和注册表值；
   清单之外的陌生文件保留并提示（MSI 的 Component 表、Inno Setup 的 unins000.dat 同理）。
2. **删前验明身份**：快捷方式读出 `TargetPath` 并比对，注册表核对 `DisplayName` /
   `InstallLocation`，对不上一律跳过。
3. **临时副本自删**：运行中的 exe 删不掉自己，所以把自己复制到 `%TEMP%` 再以副本身份
   收尾——副本等原进程退出后删掉安装目录，最后删自己。这比「批处理里 rmdir」可靠。
"""

from __future__ import annotations

import argparse
import ctypes
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

from . import app_meta, cleanup, diag, manifest, registry_ops, shortcuts  # noqa: E402

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102


def running_exe() -> Path:
    """当前进程对应的可执行文件（打包后是 Uninstall.exe / ChatClientSetup.exe）。"""
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
    """删除文件或目录（递归），遇到占用/只读重试几次；返回是否删干净。

    注意：既要能删目录也要能删单个文件——收尾阶段要删的是 `Uninstall.exe`，
    早期版本只调 `shutil.rmtree()`，对文件会抛 `NotADirectoryError`，
    表现为「卸载后 Uninstall.exe 一直留着」。
    """
    if not path.exists():
        return True
    for attempt in range(1, attempts + 1):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            log("* 第 {} 次删除 {} 失败: {}".format(attempt, path, exc))
            _clear_readonly(path)
            time.sleep(0.6)
    return not path.exists()


def _clear_readonly(path: Path) -> None:
    if path.is_file():
        try:
            os.chmod(path, 0o700)
        except OSError:
            return
        return
    for child in path.rglob("*"):
        try:
            os.chmod(child, 0o700)
        except OSError:
            continue


def expected_shortcuts(install_root: Path) -> list[tuple[Path, Path]]:
    """没有清单的旧版安装：按固定命名推算三个快捷方式及其期望目标。"""
    desktop = app_meta.desktop_dir()
    start_menu = app_meta.start_menu_dir()
    folder = start_menu / app_meta.START_MENU_FOLDER
    return [
        (desktop / app_meta.client_shortcut_name(), app_meta.exe_path(install_root)),
        (folder / app_meta.client_shortcut_name(), app_meta.exe_path(install_root)),
        (folder / app_meta.uninstall_shortcut_name(), app_meta.uninstaller_path(install_root)),
    ]


def remove_shortcuts(install_root: Path, entries: list[tuple[Path, Path]] | None, log=print) -> list[Path]:
    """删除快捷方式，删除前核对 .lnk 的目标；目标不符或读不出来就跳过。"""
    failures: list[Path] = []
    for link, expected in entries or expected_shortcuts(install_root):
        actual = shortcuts.read_shortcut_target(link)
        if actual is None:
            continue  # 不存在（或读不出来）——不存在视为已清理
        try:
            matches = Path(actual).resolve() == Path(expected).resolve()
        except OSError:
            matches = False
        if not matches:
            log("* 跳过快捷方式（目标不是本程序）: {} → {}".format(link, actual))
            continue
        if not shortcuts.remove_shortcut(link):
            failures.append(link)
    shortcuts.remove_empty_dir(app_meta.start_menu_dir() / app_meta.START_MENU_FOLDER)
    return failures


def remaining_unknown(install_root: Path, self_path: Path | None = None) -> list[Path]:
    """安装目录里清单之外的东西（自删模式下把自己的 exe 排除）。"""
    root = Path(install_root)
    if not root.exists():
        return []
    own = {
        app_meta.CLIENT_EXE.lower(),
        app_meta.UNINSTALL_EXE.lower(),
        app_meta.INSTALL_MARKER.lower(),
        manifest.MANIFEST_NAME.lower(),
    }
    unknown: list[Path] = []
    for entry in root.iterdir():
        if entry.name.lower() in own:
            continue
        if self_path is not None:
            try:
                if entry.resolve() == Path(self_path).resolve():
                    continue
            except OSError:
                pass
        unknown.append(entry)
    return unknown


def wait_for_process_exit(pid: int, timeout: float = 30.0) -> None:
    """等指定进程退出（临时副本用它等原卸载器释放文件）。"""
    if pid <= 0:
        return
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return
    try:
        kernel32.WaitForSingleObject(handle, int(timeout * 1000))
    finally:
        kernel32.CloseHandle(handle)


def spawn_temp_cleanup(install_root: Path, log=print) -> Path | None:
    """把自己复制到 %TEMP% 并以 `--cleanup` 模式启动，用来删除安装目录和自身。"""
    program = running_exe()
    if not getattr(sys, "frozen", False):
        return None  # 源码模式没有 exe 可复制，交给调用方直接删
    temp_dir = Path(tempfile.gettempdir())
    temp_exe = temp_dir / "python-chat-client-uninstall-{}.exe".format(os.getpid())
    try:
        shutil.copy2(program, temp_exe)
    except OSError as exc:
        log("! 无法创建临时卸载副本: {}".format(exc))
        return None
    try:
        subprocess.Popen(
            [str(temp_exe), "--cleanup", str(install_root), "--parent-pid", str(os.getpid())],
            creationflags=_DETACHED,
            close_fds=True,
            cwd=str(temp_dir),
        )
    except OSError as exc:
        log("! 无法启动临时卸载副本: {}".format(exc))
        return None
    return temp_exe


def schedule_self_delete(path: Path) -> None:
    """删除临时副本自己（cmd 延迟一点，避免文件仍被占用）。"""
    script = 'ping -n 3 127.0.0.1 > nul & del /f /q "{}"'.format(path)
    try:
        subprocess.Popen(
            ["cmd", "/c", script],
            creationflags=_DETACHED,
            close_fds=True,
            cwd=str(Path(tempfile.gettempdir())),
        )
    except OSError:
        return


def cleanup_after_exit(install_root: Path, parent_pid: int, log=print) -> int:
    """临时副本的收尾：等原卸载器退出 → 删安装目录 → 删自己。"""
    wait_for_process_exit(parent_pid)
    remove_tree(install_root / app_meta.UNINSTALL_EXE, log)
    for name in (app_meta.INSTALL_MARKER, manifest.MANIFEST_NAME):
        try:
            (install_root / name).unlink()
        except OSError:
            pass
    for artifact in cleanup.runtime_artifacts(install_root):
        if artifact.is_dir():
            remove_tree(artifact, log)
        else:
            try:
                artifact.unlink()
            except OSError:
                pass
    if install_root.exists():
        unknown = [item for item in install_root.iterdir()]
        if unknown:
            log("* 安装目录里还有清单之外的文件，保留: {}".format(install_root))
            log("  {}".format("、".join(item.name for item in unknown[:5])))
        else:
            try:
                install_root.rmdir()
                log("* 已删除安装目录: {}".format(install_root))
            except OSError as exc:
                log("! 删除安装目录失败: {}".format(exc))
    if getattr(sys, "frozen", False):
        schedule_self_delete(running_exe())
    return 0


def leftovers(install_root: Path, data_root: Path, desktop: Path, start_menu: Path) -> list[str]:
    """自检：列出仍然存在的痕迹（空列表 = 卸载干净）。"""
    remaining = []
    for path in cleanup.removal_plan(install_root, data_root, desktop, start_menu):
        if path.exists():
            remaining.append(str(path))
    if registry_ops.matches_install(install_root):
        remaining.append("HKCU\\{}".format(app_meta.UNINSTALL_KEY))
    return remaining


def uninstall(
    install_root: Path,
    keep_data: bool = False,
    log=print,
    inside_running_dir: bool | None = None,
) -> int:
    """执行卸载；返回 0 表示成功（含「已交给临时副本收尾」），1 表示仍有残留。"""
    install_root = Path(install_root)
    data_from_manifest = None
    if not cleanup.is_safe_install_root(install_root):
        log("! 安装目录不安全，已拒绝卸载: {}".format(install_root))
        return 1

    if not cleanup.looks_like_install_dir(install_root):
        # 这是本代理误删事件的直接教训：没有清单/标记的目录一律不删。
        log("! 目标目录不像本程序的安装目录（缺少 {} 或两个 exe），已拒绝卸载: {}".format(
            manifest.MANIFEST_NAME, install_root
        ))
        log("  如确认要卸载，请用 --dir 指向真正的安装目录（{}）。".format(app_meta.install_dir()))
        return 1

    data = manifest.read_manifest(install_root)
    shortcuts_list = manifest.shortcut_entries(data) if data else None
    if data:
        data_from_manifest = manifest.data_dir_of(data)

    kill_running_client(log)

    for link in remove_shortcuts(install_root, shortcuts_list, log):
        log("! 快捷方式没删掉: {}".format(link))

    if keep_data:
        log("* 保留用户数据目录（按 --keep-data）: {}".format(app_meta.data_dir()))
    else:
        data_root = data_from_manifest or app_meta.data_dir()
        if remove_tree(data_root, log):
            log("* 已删除用户数据目录: {}".format(data_root))
        else:
            log("! 用户数据目录删除不完整: {}".format(data_root))

    if inside_running_dir is None:
        inside_running_dir = cleanup.is_inside(running_exe().parent, install_root)

    # ---- 文件：清单驱动；没有清单时退回「旧版：除自己之外都删」 ----
    if data:
        files = manifest.listed_files(data, install_root)
        removed = 0
        for path in files:
            if not path.exists():
                continue
            try:
                if path.is_dir():
                    remove_tree(path, log)
                else:
                    path.unlink()
                removed += 1
            except OSError as exc:
                log("! 删除失败 {}: {}".format(path, exc))
        log("* 已按清单删除 {} 个文件/目录".format(removed))
    else:
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
        log("* 旧版安装（无清单）：已删除安装目录内除卸载器外的内容")

    for artifact in cleanup.runtime_artifacts(install_root):
        if artifact.is_dir():
            remove_tree(artifact, log)
        else:
            try:
                artifact.unlink()
            except OSError:
                pass

    # ---- 注册表：核对身份后再删 ----
    if data:
        if registry_ops.matches_install(install_root):
            registry_ops.delete_uninstall_key()
            log("* 已删除卸载注册表项")
        elif registry_ops.read_values():
            log("* 跳过注册表项（DisplayName/InstallLocation 与本程序不符）")
    else:
        registry_ops.delete_uninstall_key()

    if inside_running_dir:
        temp_exe = spawn_temp_cleanup(install_root, log)
        if temp_exe is not None:
            log("* 已交给临时副本收尾，安装目录稍后会被删除")
            return 0

    unknown = remaining_unknown(install_root, running_exe())
    if unknown:
        log("* 安装目录里还有清单之外的文件，保留: {}".format(install_root))
        log("  {}".format("、".join(item.name for item in unknown[:5])))
        return 0
    if remove_tree(install_root, log):
        log("* 已删除安装目录: {}".format(install_root))
    else:
        log("! 安装目录删除不完整: {}".format(install_root))
        return 1
    log("* 卸载完成，未发现残留")
    return 0


# ------------------------------------------------------------------ 图形向导


def run_gui(install_root: Path, keep_data: bool) -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("卸载 {}".format(app_meta.DISPLAY_NAME))
    root.geometry("580x300+220+180")
    root.resizable(False, False)

    ttk.Label(
        root, text="卸载 {}".format(app_meta.DISPLAY_NAME), font=("Microsoft YaHei", 14, "bold")
    ).pack(anchor=tk.W, padx=18, pady=(16, 4))
    ttk.Label(root, text="安装目录：{}".format(install_root), wraplength=540).pack(anchor=tk.W, padx=18)
    ttk.Label(
        root,
        text=(
            "将按安装清单删除：程序文件、快捷方式、用户配置与日志、注册表卸载项；"
            "清单之外的陌生文件会保留。"
        ),
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

    lines: list[str] = []

    def do_uninstall() -> None:
        if not messagebox.askyesno("确认卸载", "确定要卸载 {} 吗？".format(app_meta.DISPLAY_NAME)):
            return
        confirm.config(state=tk.DISABLED)
        status.set("正在卸载…")
        root.update_idletasks()
        del lines[:]
        code = uninstall(install_root, keep_data=keep_var.get(), log=lines.append)
        if lines:
            diag.log("\n".join(lines))
        if code == 0:
            note = ""
            if install_root.exists():
                note = (
                    "\n\n已按安装清单清理完毕；安装目录里剩下的内容不在清单里（或只剩卸载器自己），"
                    "确认无误后可直接拖进回收站。"
                )
            messagebox.showinfo("卸载完成", "已卸载并清理干净。" + note)
            root.destroy()
        else:
            reason = next(
                (line.lstrip("! ").strip() for line in reversed(lines) if line.strip().startswith("!")),
                "仍有残留。",
            )
            status.set("卸载未完全成功")
            confirm.config(state=tk.NORMAL)
            messagebox.showwarning("卸载未完成", "{}\n\n请查看日志后重试。".format(reason))

    confirm.config(command=do_uninstall)
    confirm.pack(side=tk.RIGHT)
    ttk.Button(buttons, text="取消", command=root.destroy).pack(side=tk.RIGHT, padx=8)
    root.mainloop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="卸载 {}".format(app_meta.DISPLAY_NAME))
    parser.add_argument("--silent", action="store_true", help="不显示界面，直接卸载")
    parser.add_argument("--dir", default="", help="安装目录（默认从注册表或清单读取）")
    parser.add_argument("--keep-data", action="store_true", help="保留 %%APPDATA%% 下的用户配置")
    parser.add_argument("--cleanup", default="", help=argparse.SUPPRESS)  # 临时副本内部使用
    parser.add_argument("--parent-pid", type=int, default=0, help=argparse.SUPPRESS)
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
    if args.cleanup:
        return cleanup_after_exit(Path(args.cleanup), args.parent_pid)
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
