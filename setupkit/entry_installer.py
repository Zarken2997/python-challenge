"""源码模式入口：安装（等价于 `python -m setupkit.app`）。

打包产物只有一个 exe（`setupkit/entry_setup.py`），安装/卸载靠 `--uninstall` 切换；
这个文件只是为了在源码树里方便地单独跑安装流程。
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from setupkit.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
