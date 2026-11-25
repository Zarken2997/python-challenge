"""统一入口：安装包和卸载器是同一个程序（主流做法是「一个 exe + 一个 --uninstall 开关」）。

| 启动方式 | 行为 |
| --- | --- |
| `ChatClientSetup.exe` | 安装向导 |
| `ChatClientSetup.exe --uninstall` | 卸载向导（安装时复制的 `Uninstall.exe` 就是这样被调起的） |
| `... --silent [--uninstall]` | 静默安装 / 静默卸载（自动化与批量部署用） |
| `... --cleanup <目录> --parent-pid N` | 临时副本收尾，内部使用，不出现在帮助里 |
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):  # 允许直接 `python setupkit\\app.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "setupkit"

from . import app_meta, diag, installer, uninstaller  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="{} 安装与卸载（同一个程序，用 --uninstall 切换）".format(app_meta.DISPLAY_NAME)
    )
    parser.add_argument("--uninstall", action="store_true", help="卸载模式（默认是安装）")
    parser.add_argument("--silent", action="store_true", help="不显示界面，直接执行")
    parser.add_argument("--dir", default="", help="安装目录（安装时默认为 %%LOCALAPPDATA%% 下的位置）")
    parser.add_argument("--no-desktop", action="store_true", help="安装时不创建桌面快捷方式")
    parser.add_argument("--no-startmenu", action="store_true", help="安装时不创建开始菜单快捷方式")
    parser.add_argument("--run", action="store_true", help="安装完成后启动客户端")
    parser.add_argument("--keep-data", action="store_true", help="卸载时保留 %%APPDATA%% 下的用户配置")
    parser.add_argument("--cleanup", default="", help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, default=0, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cleanup:
            return uninstaller.cleanup_after_exit(Path(args.cleanup), args.parent_pid)

        if args.uninstall:
            target = uninstaller.resolve_install_root(args.dir)
            if target is None:
                message = "找不到安装目录，请用 --dir 指定"
                if not args.silent:
                    uninstaller.show_error("卸载失败", message)
                print("! " + message)
                return 1
            diag.log("卸载开始: dir={} silent={}".format(target, args.silent))
            code = (
                uninstaller.uninstall(target, keep_data=args.keep_data)
                if args.silent
                else uninstaller.run_gui(target, keep_data=args.keep_data)
            )
            diag.log("卸载结束: code={}".format(code))
            return code

        install_root = Path(args.dir) if args.dir.strip() else app_meta.install_dir()
        diag.log("安装开始: dir={} silent={}".format(install_root, args.silent))
        code = (
            installer.install(
                install_root,
                desktop=not args.no_desktop,
                start_menu=not args.no_startmenu,
            )
            if args.silent
            else installer.run_gui(install_root, run_after=args.run)
        )
        diag.log("安装结束: code={}".format(code))
        return code
    except Exception:  # noqa: BLE001 - 顶层兜底，保证日志里有完整堆栈
        diag.log_exception("安装/卸载")
        message = "出现异常，详情见日志：\n{}".format(diag.log_path())
        if not args.silent:
            installer.show_error("操作失败", message)
        print("! " + message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
