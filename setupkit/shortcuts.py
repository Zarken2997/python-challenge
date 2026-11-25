"""快捷方式创建与删除。

用 Windows 自带的 PowerShell（WScript.Shell COM）来写 .lnk，避免为了几十字节的 COM
接口再引入一个二进制依赖；调用失败不影响主流程（安装器会提示但继续，卸载器照样删）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run_powershell(script: str, timeout: int = 30) -> tuple[bool, str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    ok = completed.returncode == 0
    detail = (completed.stderr or completed.stdout or "").strip()
    return ok, detail


def _ps_quote(text: str) -> str:
    """PowerShell 单引号字符串：内部的单引号要写两次。"""
    return "'" + str(text).replace("'", "''") + "'"


def create_shortcut(
    link_path: Path,
    target: Path,
    arguments: str = "",
    workdir: Path | None = None,
    icon: Path | None = None,
    description: str = "",
) -> tuple[bool, str]:
    """创建 .lnk；父目录不存在时先创建。返回 (是否成功, 错误信息)。"""
    try:
        link_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, str(exc)
    icon_location = str(icon or target)
    script = (
        "$ErrorActionPreference='Stop';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut({link});"
        "$s.TargetPath={target};"
        "$s.Arguments={args};"
        "$s.WorkingDirectory={workdir};"
        "$s.IconLocation={icon};"
        "$s.Description={desc};"
        "$s.Save()"
    ).format(
        link=_ps_quote(str(link_path)),
        target=_ps_quote(str(target)),
        args=_ps_quote(arguments),
        workdir=_ps_quote(str(workdir or target.parent)),
        icon=_ps_quote(icon_location),
        desc=_ps_quote(description),
    )
    return _run_powershell(script)


def remove_shortcut(link_path: Path) -> bool:
    """删除 .lnk（不存在视为成功，卸载要能重复执行）。"""
    try:
        link_path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        # 被占用时再试一次强制删除
        ok, _ = _run_powershell("Remove-Item -LiteralPath {} -Force".format(_ps_quote(str(link_path))))
        return ok
    return True


def remove_empty_dir(path: Path) -> None:
    """删掉空目录（如开始菜单文件夹），非空或不存在都静默跳过。"""
    try:
        path.rmdir()
    except OSError:
        return
