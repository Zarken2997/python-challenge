"""安装/卸载过程的诊断日志。

安装包是 GUI 程序（没有控制台），出错时只有弹窗，排障很不方便；这里统一把
每一步和异常写入 `%TEMP%\\PythonChatClient-setup.log`，用户可以把文件发给开发者。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

LOG_NAME = "PythonChatClient-setup.log"


def log_path() -> Path:
    override = os.environ.get("PYTHON_CHAT_SETUP_LOG")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / LOG_NAME


def log(message: str) -> None:
    """追加一行带时间戳的日志；写不进去也不能影响主流程。"""
    line = "{} {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        with open(str(log_path()), "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass
    stream = sys.stdout
    if stream is not None:
        try:
            stream.write(line)
            stream.flush()
        except (OSError, ValueError):
            pass


def log_exception(context: str) -> None:
    log("!! {} 失败\n{}".format(context, traceback.format_exc()))
