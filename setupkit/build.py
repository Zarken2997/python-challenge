"""构建 Windows 安装包（安装 + 卸载二合一）。

与主流安装器一致，只产出一个可执行文件：

1. `ChatClient.exe`：GUI 客户端（单文件、无控制台窗口）；
2. `ChatClientSetup.exe`：安装包，内嵌 `ChatClient.exe`；安装时把自己复制成
   `<安装目录>\\Uninstall.exe`，卸载就是同一个程序加 `--uninstall`。

用法（必须用装了 PyInstaller 的解释器执行）：
    .venv-pack\\Scripts\\python.exe setupkit\\build.py
产物：dist\\ChatClientSetup.exe、dist\\app\\ChatClient.exe
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # 允许直接 `python setupkit\\build.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "setupkit"

from . import app_meta  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST = REPO_ROOT / "dist"
BUILD = REPO_ROOT / "build" / "pyinstaller"
SPECS = REPO_ROOT / "build" / "specs"


def pyinstaller(*args: str) -> None:
    """调用当前解释器的 PyInstaller 模块，失败时抛出 CalledProcessError。"""
    command = [sys.executable, "-m", "PyInstaller", *args]
    print("> " + " ".join(command), flush=True)
    subprocess.run(command, cwd=str(REPO_ROOT), check=True)


def common_args(name: str) -> list[str]:
    return [
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        name,
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD / name),
        "--specpath",
        str(SPECS),
        "--paths",
        str(REPO_ROOT),
    ]


def build_client() -> Path:
    pyinstaller(*common_args("ChatClient"), str(REPO_ROOT / "setupkit" / "entry_client.py"))
    return DIST / "ChatClient.exe"


def stage_payload(client: Path) -> Path:
    """把客户端放进 dist/app，安装包把它当数据一起打包（卸载器由安装包自身复制）。"""
    payload = DIST / "app"
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)
    shutil.copy2(client, payload / app_meta.CLIENT_EXE)
    return payload


def build_setup(payload: Path) -> Path:
    pyinstaller(
        *common_args(app_meta.SETUP_EXE.rsplit(".", 1)[0]),
        "--add-data",
        "{};app".format(payload),
        str(REPO_ROOT / "setupkit" / "entry_setup.py"),
    )
    return DIST / app_meta.SETUP_EXE


def human_size(path: Path) -> str:
    size = path.stat().st_size
    return "{:.1f} MB".format(size / (1024 * 1024))


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    client = build_client()
    payload = stage_payload(client)
    setup = build_setup(payload)
    print("\n构建完成：")
    for path in (setup, client):
        print("  {:<24} {}".format(path.name, human_size(path)))
    old_uninstaller = DIST / app_meta.UNINSTALL_EXE
    if old_uninstaller.exists():
        print("  提示：dist\\{} 是旧版独立卸载器，新版不再单独构建，可删除。".format(app_meta.UNINSTALL_EXE))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
