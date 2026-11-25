"""安装清单：安装器记录「自己创建了什么」，卸载器只删清单里的东西。

主流安装器都是这么做的：MSI 用 Component 表，Inno Setup 用 `unins000.dat` 中的安装
动作日志，卸载时逐条回放。这里用一份 JSON 文件实现同样语义——卸载时不再递归删除
整个安装目录，而是按清单逐项核验、逐项删除，清单之外的陌生文件一律保留。

清单字段：

```json
{
  "app_id": "PythonChatClient",
  "version": "1.0.0",
  "installed_at": "2026-11-25 11:30:00",
  "install_root": "C:\\\\Users\\\\me\\\\AppData\\\\Local\\\\Programs\\\\PythonChatClient",
  "files": ["ChatClient.exe", "Uninstall.exe"],
  "shortcuts": [{"path": "...", "target": "..."}],
  "registry": {"key": "Software\\\\...", "values": {...}},
  "data_dir": "C:\\\\Users\\\\me\\\\AppData\\\\Roaming\\\\PythonChatClient"
}
```

`files` 用相对安装目录的路径；读取时会拒绝绝对路径与跑出安装目录的 `..`，避免清单被
篡改后把删除范围扩到目录之外。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import app_meta, cleanup

MANIFEST_NAME = "install-manifest.json"


def manifest_path(install_root: Path) -> Path:
    """清单文件的位置：安装目录下的 `install-manifest.json`。"""
    return Path(install_root) / MANIFEST_NAME


def make_manifest(
    install_root: Path,
    files: list[str],
    shortcuts: list[dict[str, str]],
    registry_values: dict[str, object] | None = None,
    data_dir: Path | None = None,
) -> dict[str, object]:
    """拼出清单结构（值都可 JSON 序列化，int 会原样保留）。"""
    return {
        "app_id": app_meta.APP_ID,
        "version": app_meta.VERSION,
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "install_root": str(Path(install_root)),
        "files": [str(name) for name in files],
        "shortcuts": [
            {"path": str(item["path"]), "target": str(item["target"])} for item in shortcuts
        ],
        "registry": {
            "key": app_meta.UNINSTALL_KEY,
            "values": dict(registry_values or {}),
        },
        "data_dir": str(data_dir or app_meta.data_dir()),
    }


def write_manifest(
    install_root: Path,
    files: list[str],
    shortcuts: list[dict[str, str]],
    registry_values: dict[str, object] | None = None,
    data_dir: Path | None = None,
) -> Path:
    """写清单文件；返回写入的路径。"""
    data = make_manifest(install_root, files, shortcuts, registry_values, data_dir)
    path = manifest_path(install_root)
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def read_manifest(install_root: Path) -> dict[str, object] | None:
    """读清单；文件缺失、不是 JSON、或 app_id 对不上时返回 None（按旧版安装处理）。"""
    try:
        text = manifest_path(install_root).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("app_id") != app_meta.APP_ID:
        return None
    return data


def listed_files(manifest: dict[str, object], install_root: Path) -> list[Path]:
    """清单里记录的文件，解析成绝对路径；越界或非法的条目直接丢弃。"""
    root = Path(install_root)
    result: list[Path] = []
    raw = manifest.get("files")
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        candidate = Path(item)
        if candidate.is_absolute():
            continue  # 只接受相对安装目录的路径
        resolved = (root / candidate).resolve()
        if not cleanup.is_inside(resolved, root):
            continue
        result.append(resolved)
    return result


def shortcut_entries(manifest: dict[str, object]) -> list[tuple[Path, Path]]:
    """清单里的快捷方式 (路径, 期望目标)；目标用于删除前核对身份。"""
    result: list[tuple[Path, Path]] = []
    raw = manifest.get("shortcuts")
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        target = item.get("target")
        if isinstance(path, str) and path.strip() and isinstance(target, str) and target.strip():
            result.append((Path(path), Path(target)))
    return result


def registry_values(manifest: dict[str, object]) -> dict[str, object]:
    """清单里记录的注册表值（键名固定为 `app_meta.UNINSTALL_KEY` 时才算数）。"""
    raw = manifest.get("registry")
    if not isinstance(raw, dict):
        return {}
    if raw.get("key") != app_meta.UNINSTALL_KEY:
        return {}
    values = raw.get("values")
    return values if isinstance(values, dict) else {}


def data_dir_of(manifest: dict[str, object]) -> Path | None:
    """清单里记录的用户数据目录；只接受本程序的标准位置，避免指向别处。"""
    raw = manifest.get("data_dir")
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = Path(raw)
    try:
        if candidate.resolve() != app_meta.data_dir().resolve():
            return None
    except OSError:
        return None
    return candidate
