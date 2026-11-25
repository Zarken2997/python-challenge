"""卸载清单与安全校验：决定「卸载时要删哪些东西」，以及「哪些路径绝对不能删」。

这里是纯函数，不碰磁盘，方便单元测试直接断言清单内容。
"""

from __future__ import annotations

import os
from pathlib import Path

from . import app_meta

#: 这些目录本身（或其父级）绝不能被递归删除
_PROTECTED_NAMES = {
    "windows",
    "system32",
    "syswow64",
    "program files",
    "program files (x86)",
    "users",
    "appdata",
    "local",
    "roaming",
    "desktop",
    "documents",
    "downloads",
    "microsoft",
    "start menu",
}


def shortcut_files(install_root: Path, desktop: Path, start_menu: Path) -> list[Path]:
    """安装器创建的快捷方式清单（卸载时逐个删）。"""
    return [
        desktop / app_meta.client_shortcut_name(),
        start_menu / app_meta.START_MENU_FOLDER / app_meta.client_shortcut_name(),
        start_menu / app_meta.START_MENU_FOLDER / app_meta.uninstall_shortcut_name(),
    ]


def removal_plan(install_root: Path, data_root: Path, desktop: Path, start_menu: Path) -> list[Path]:
    """按「先文件后目录、先浅后深」的顺序返回要删除的路径。

    卸载器按这个顺序逐个删；目录用 rmdir / 递归删除，因此深度顺序很重要。
    """
    return [
        *shortcut_files(install_root, desktop, start_menu),
        data_root,
        install_root,
    ]


def is_safe_install_root(path: Path) -> bool:
    """拒绝明显危险的安装目录，避免卸载器把系统目录当成应用目录删掉。

    规则：必须是绝对路径、至少两级、名字不在保护名单里、且不是某个盘的根。
    """
    if not path.is_absolute():  # 相对路径会随当前目录漂移，绝不能作为安装根
        return False
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    parts = [part for part in resolved.parts if part not in ("", os.sep)]
    if not resolved.is_absolute() or len(parts) < 2:
        return False
    if resolved.parent == resolved:  # 盘符根目录
        return False
    name = resolved.name.strip().lower()
    if name in _PROTECTED_NAMES:
        return False
    if name.startswith("."):
        return False
    if resolved.parent.name.strip().lower() in _PROTECTED_NAMES:
        # 例如 C:\Users\demo、C:\Windows\Temp 这类「保护目录的直接子目录」，不做安装根
        return False
    return True


def has_install_marker(root: Path) -> bool:
    """安装目录里是否有本程序写下的标记文件（内容需含 `APP_ID`）。

    标记文件由安装器写入，普通目录里不会有，因此可以据此判断
    「这个目录是不是我装的」，避免卸载器误删无关目录。
    """
    try:
        text = app_meta.marker_path(root).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return app_meta.APP_ID in text


def looks_like_install_dir(root: Path) -> bool:
    """目录像不像本程序的安装目录：有标记文件，或同时有客户端与卸载器两个 exe。"""
    if has_install_marker(root):
        return True
    try:
        return app_meta.exe_path(root).is_file() and app_meta.uninstaller_path(root).is_file()
    except OSError:
        return False


def is_inside(child: Path, parent: Path) -> bool:
    """child 是否位于 parent 之内（用于确认卸载器自己就在安装目录里）。"""
    try:
        child_resolved = child.expanduser().resolve()
        parent_resolved = parent.expanduser().resolve()
    except OSError:
        return False
    return parent_resolved == child_resolved or parent_resolved in child_resolved.parents


def app_process_names() -> tuple[str, ...]:
    """卸载前需要结束的进程名。"""
    return (app_meta.CLIENT_EXE,)
