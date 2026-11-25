"""安装包元信息：应用名、版本、默认安装位置、注册表键。

安装器、卸载器、构建脚本都从这里取常量，避免三处各写一份导致「装的地方和卸的地方不一致」。
所有函数都允许传入 base 目录，便于测试时不碰真实用户目录。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_ID = "PythonChatClient"
DISPLAY_NAME = "任务1 聊天客户端"
VERSION = "1.0.0"
PUBLISHER = "Python 挑战课题"

CLIENT_EXE = "ChatClient.exe"
UNINSTALL_EXE = "Uninstall.exe"
SETUP_EXE = "ChatClientSetup.exe"

# HKCU 下无需管理员权限，卸载项会出现在「设置 → 应用 → 已安装的应用」
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\PythonChatClient"
START_MENU_FOLDER = "任务1 聊天客户端"

#: 安装后由程序自身可能产生的文件（卸载时一并清理）
RUNTIME_ARTIFACTS = ("client.log", "logs", "config.json")

#: 安装时写入安装目录的标记文件，卸载器据此确认「这确实是我装的目录」。
#: 有了它，误把仓库目录或 F:\codex 之类的路径当安装目录时会被直接拒绝。
INSTALL_MARKER = "install-info.txt"


def _env_path(name: str, fallback: str = "") -> Path:
    value = os.environ.get(name) or fallback
    return Path(value)


def local_app_data() -> Path:
    return _env_path("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))


def roaming_app_data() -> Path:
    return _env_path("APPDATA", str(Path.home() / "AppData" / "Roaming"))


def install_dir() -> Path:
    """默认安装目录：当前用户、免管理员。"""
    return local_app_data() / "Programs" / APP_ID


def data_dir() -> Path:
    """运行期数据目录（配置、日志），卸载时按用户选择删除。"""
    return roaming_app_data() / APP_ID


def desktop_dir() -> Path:
    """桌面目录：优先读注册表里的 Shell Folders（兼容 OneDrive 重定向）。"""
    return _shell_folder("Desktop", Path.home() / "Desktop")


def start_menu_dir() -> Path:
    """开始菜单 Programs 目录。"""
    return _shell_folder(
        "Programs",
        roaming_app_data() / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    )


def _shell_folder(name: str, fallback: Path) -> Path:
    """从 HKCU\\...\\Explorer\\Shell Folders 读取系统目录，读不到就用默认值。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return fallback
    if isinstance(value, str) and value.strip():
        return Path(os.path.expandvars(value))
    return fallback


def client_shortcut_name() -> str:
    return "{} 客户端.lnk".format(DISPLAY_NAME)


def uninstall_shortcut_name() -> str:
    """开始菜单里的卸载快捷方式，方便用户不经「设置」也能卸载。"""
    return "卸载 {}.lnk".format(DISPLAY_NAME)


def exe_path(root: Path) -> Path:
    return root / CLIENT_EXE


def uninstaller_path(root: Path) -> Path:
    return root / UNINSTALL_EXE


def marker_path(root: Path) -> Path:
    """安装标记文件路径（内容和 `APP_ID` 一起校验）。"""
    return root / INSTALL_MARKER
