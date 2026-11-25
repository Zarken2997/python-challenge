"""构建 Windows 安装包与卸载程序。

步骤：
1. `ChatClient.exe`：GUI 客户端（单文件，无控制台窗口）；
2. `Uninstall.exe`：卸载程序（单文件，无控制台窗口）；
3. `ChatClientSetup.exe`：安装包，内嵌上面两个 exe，运行后给出安装向导。

用法（必须用装了 PyInstaller 的解释器执行）：
    .venv-pack\\Scripts\\python.exe setupkit\\build.py
产物：dist\\ChatClientSetup.exe、dist\\app\\{ChatClient.exe,Uninstall.exe}
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


def build_uninstaller() -> Path:
    pyinstaller(*common_args("Uninstall"), str(REPO_ROOT / "setupkit" / "entry_uninstaller.py"))
    return DIST / "Uninstall.exe"


def stage_payload(client: Path, uninstaller: Path) -> Path:
    """把两个 exe 放进 dist/app，安装包会把这些文件当作数据一起打包。"""
    payload = DIST / "app"
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)
    shutil.copy2(client, payload / app_meta.CLIENT_EXE)
    shutil.copy2(uninstaller, payload / app_meta.UNINSTALL_EXE)
    return payload


def build_setup(payload: Path) -> Path:
    pyinstaller(
        *common_args("ChatClientSetup"),
        "--add-data",
        "{};app".format(payload),
        str(REPO_ROOT / "setupkit" / "entry_installer.py"),
    )
    return DIST / "{}.exe".format("ChatClientSetup")


def human_size(path: Path) -> str:
    size = path.stat().st_size
    return "{:.1f} MB".format(size / (1024 * 1024))


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    client = build_client()
    uninstaller = build_uninstaller()
    payload = stage_payload(client, uninstaller)
    setup = build_setup(payload)
    print("\n构建完成：")
    for path in (setup, client, uninstaller):
        print("  {:<24} {}".format(path.name, human_size(path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
