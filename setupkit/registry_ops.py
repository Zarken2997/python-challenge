"""注册表操作：写入/删除 HKCU 卸载项，让「设置 → 应用」里能看到并卸载本程序。

只动 HKEY_CURRENT_USER 下的一个键，不需要管理员权限，也不碰系统其它位置。
"""

from __future__ import annotations

import winreg
from pathlib import Path

from . import app_meta


def uninstall_values(
    install_root: Path,
    version: str = app_meta.VERSION,
    display_name: str = app_meta.DISPLAY_NAME,
    publisher: str = app_meta.PUBLISHER,
    estimated_kb: int = 0,
    install_date: str = "",
) -> dict[str, object]:
    """构造卸载项的全部键值（纯数据，便于测试逐项断言）。"""
    exe = app_meta.exe_path(install_root)
    uninstaller = app_meta.uninstaller_path(install_root)
    return {
        "DisplayName": display_name,
        "DisplayVersion": version,
        "Publisher": publisher,
        "DisplayIcon": str(exe),
        "InstallLocation": str(install_root),
        "UninstallString": '"{}"'.format(uninstaller),
        "QuietUninstallString": '"{}" --silent'.format(uninstaller),
        "InstallDate": install_date,
        "EstimatedSize": estimated_kb,
        "NoModify": 1,
        "NoRepair": 1,
    }


def write_uninstall_key(
    install_root: Path,
    values: dict[str, object] | None = None,
    key_path: str = app_meta.UNINSTALL_KEY,
) -> None:
    """写入（覆盖）卸载项。EstimatedSize / NoModify 等必须是整数。"""
    payload = values if values is not None else uninstall_values(install_root)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
        for name, value in payload.items():
            if isinstance(value, int):
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))


def read_values(key_path: str = app_meta.UNINSTALL_KEY) -> dict[str, object]:
    """读取卸载项；键不存在时返回空字典（测试与卸载器自检都用它）。"""
    values: dict[str, object] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            count = winreg.QueryInfoKey(key)[1]
            for index in range(count):
                name, value, _kind = winreg.EnumValue(key, index)
                values[name] = value
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    return values


def delete_uninstall_key(key_path: str = app_meta.UNINSTALL_KEY) -> bool:
    """删除卸载项；不存在时返回 False 而不是抛异常。"""
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def install_location(key_path: str = app_meta.UNINSTALL_KEY) -> Path | None:
    """从注册表取安装目录，供卸载器在自身位置被移动后仍能找到目标。"""
    value = read_values(key_path).get("InstallLocation")
    if isinstance(value, str) and value.strip():
        return Path(value)
    return None
