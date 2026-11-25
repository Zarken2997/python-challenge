"""PyInstaller 打包入口：安装包 / 卸载器二合一（同一个 exe，用 `--uninstall` 切换）。"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from setupkit.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
