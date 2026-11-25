# setupkit —— 客户端安装包（安装 + 卸载二合一）

把任务 1 的聊天客户端打包成**一个安装包**（`ChatClientSetup.exe`）。双击安装后带桌面 / 开始菜单
快捷方式和「设置 → 应用」里的卸载项；卸载就是**同一个 exe 加 `--uninstall`**，按安装清单把
快捷方式、程序目录、用户数据目录和注册表项逐项清掉。

整体思路对齐主流安装器（MSI / Inno Setup）：

- **安装清单**（`install-manifest.json`）记录本次安装写了哪些文件、快捷方式、注册表值、数据目录；
- **卸载按清单回放**，删之前先核验身份（快捷方式看目标路径、注册表项看 DisplayName/InstallLocation）；
- **自删外包**：卸载时把自己复制到 `%TEMP%`，等原进程退出后再删安装目录和那份副本——
  和 Inno Setup 的 `unins000.exe` 思路一致，因为 Windows 不允许正在运行的 exe 删除自己。

## 1. 产物

| 文件 | 大小（参考） | 作用 |
| --- | --- | --- |
| `dist/ChatClientSetup.exe` | 约 23.6 MB | 安装包；内嵌客户端，安装时把自己复制成 `<安装目录>\Uninstall.exe` |
| `dist/ChatClient.exe` | 约 11.9 MB | 客户端本体（安装包会把它放进安装目录） |
| `dist/app/ChatClient.exe` | 约 11.9 MB | 打包中间产物（安装包的载荷） |

两个 exe 都是 PyInstaller 单文件（`--onefile --windowed`），不依赖本机 Python。

## 2. 构建

```powershell
cd 'F:\codex,ultilize\python-challenge'

# 1) 建打包专用虚拟环境（只需一次）
py -3 -m venv .venv-pack

# 2) 装 PyInstaller（网络受限时可用 logs/_manual_wheels.py 离线解包 wheel）
.\.venv-pack\Scripts\python.exe -m pip install pyinstaller==6.22.3

# 3) 构建
.\.venv-pack\Scripts\python.exe setupkit\build.py
```

`build.py` 先打出 `ChatClient.exe`，放进 `dist/app/`，再用 `--add-data "dist/app;app"` 打进
`ChatClientSetup.exe`——所以安装包自带客户端，**卸载器也来自安装包自身**（安装时复制成
`Uninstall.exe`），不需要单独构建第三个 exe。

> PyInstaller 把 stderr 当错误输出，PowerShell 里可能显示 `[exit code: 1]`；
> 以日志里的 `Building EXE ... completed successfully` 和 `dist/` 里的文件为准。

## 3. 安装

双击 `dist/ChatClientSetup.exe`：确认安装目录（默认 `%LOCALAPPDATA%\Programs\PythonChatClient`，
普通用户可写，无需管理员）→ 勾选桌面 / 开始菜单快捷方式 → 「开始安装」。

静默安装：

```powershell
dist\ChatClientSetup.exe --silent --dir "D:\Tools\PythonChatClient" --no-desktop --no-startmenu
```

参数：`--silent` 不弹窗、`--dir` 指定目录、`--no-desktop` / `--no-startmenu` 不建快捷方式、
`--run` 安装后启动。

安装做了这些事：

1. 复制 `ChatClient.exe` 到安装目录；
2. 把自己复制成 `<安装目录>\Uninstall.exe`；
3. 写标记文件 `install-info.txt`（`PythonChatClient` + 版本 + 安装时间）；
4. 按需创建桌面 / 开始菜单快捷方式，并**读取每个 .lnk 的目标路径**核对是否指向本程序；
5. 写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\PythonChatClient`
   （`UninstallString` = `"<目录>\Uninstall.exe" --uninstall`）；
6. 写安装清单 `install-manifest.json`（文件列表、快捷方式及目标、注册表值、数据目录）。

> **安装包是「一次性」的**：清单、注册表、快捷方式里没有任何一条记录 `ChatClientSetup.exe`
> 的路径，所以装完就可以把安装包删掉或移走，不影响已安装的程序，也不会留下指向它的引用。
> 要卸载时用安装目录里的 `Uninstall.exe`（或「设置 → 应用」），它会按清单把东西清干净。

## 4. 卸载（重点：卸载干净）

三种入口，效果一致：

- 「设置 → 应用 → 任务1 聊天客户端 → 卸载」（走 `UninstallString`）；
- 开始菜单「卸载 任务1 聊天客户端」；
- 直接运行 `<安装目录>\Uninstall.exe`。

静默卸载：

```powershell
"%LOCALAPPDATA%\Programs\PythonChatClient\Uninstall.exe" --uninstall --silent
```

卸载流程（`setupkit/uninstaller.py`）：

1. **核验身份**：`is_safe_install_root()`（绝对路径、非盘符根、非系统目录）+ `looks_like_install_dir()`
   （有 `install-manifest.json`，或标记文件，或 `ChatClient.exe` + `Uninstall.exe`）；不符就一个文件都不删；
2. **结束进程**：`taskkill /F /IM ChatClient.exe`；
3. **删快捷方式**：只删清单里登记、且 `read_shortcut_target()` 确认指向本程序的 `.lnk`；
4. **删数据目录**：清单里的 `%APPDATA%\PythonChatClient`（`--keep-data` 保留）；
5. **删注册表项**：`matches_install()` 核对 DisplayName 与 InstallLocation 后再删；
6. **删清单里的文件**：只删 `files` 列出的项，清单之外的文件一律保留并在结论里点名；
7. **收尾**：把自己复制到 `%TEMP%\python-chat-client-uninstall-<pid>.exe`，用 ctypes
   `WaitForSingleObject` 等原进程退出后，删 `Uninstall.exe`、标记、清单、运行残留，
   目录空了就删目录，最后删掉那份临时副本。

日志写到 `%TEMP%\PythonChatClient-setup.log`（可用 `PYTHON_CHAT_SETUP_LOG` 改位置）。

### 安全护栏（每条都对应一次真实事故）

- 卸载器**不再**回退到「自己所在目录」：注册表读不到、或读到的路径不像安装目录时返回 `None`，
  提示用 `--dir` 指定（早期版本因此把 `F:\codex,ultilize` 整棵目录删掉，含 `.git`）；
- 安装器拒绝非空的外来目录，并把冲突文件名逐个列出来（放行安装包自身所在目录）；
- 卸载只看安装清单，清单之外的文件（用户自己放的笔记等）保留；
- 快捷方式按目标校验、注册表项按 DisplayName 校验，名字撞车也不会误删别人的东西。

实测（伪造目录）：

```text
! 目标目录不像本程序的安装目录（缺少安装清单/标记/两个 exe），已拒绝卸载: ...\logs\fake-project
  如确认要卸载，请用 --dir 指向真正的安装目录（C:\Users\Lenovo\AppData\Local\Programs\PythonChatClient）。
```

### 可以自己验一遍

```powershell
$dir = "$env:LOCALAPPDATA\Programs\PythonChatClient"

# 1) 安装
dist\ChatClientSetup.exe --silent --dir $dir

# 2) 检查：目录、清单、注册表项
Get-ChildItem $dir
Get-Content "$dir\install-manifest.json" -Encoding UTF8
Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PythonChatClient"

# 3) 卸载
& "$dir\Uninstall.exe" --uninstall --silent
Start-Sleep -Seconds 3

# 4) 复查：目录、数据目录、注册表项都应“不存在”
Test-Path $dir
Test-Path "$env:APPDATA\PythonChatClient"
Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PythonChatClient" -ErrorAction SilentlyContinue
```

## 5. 设计说明

- **不需要管理员**：装到 `%LOCALAPPDATA%`、只写 `HKCU`；代价是不能装到 `Program Files`
  （安装目录安全检查会拒绝盘符根、`C:\Windows`、`C:\Program Files`、`C:\Users\xxx` 这类路径）。
- **为什么清单驱动**：MSI 用 Component 表、Inno 用安装日志，都是「装了什么就删什么」；
  照抄这个思路后，卸载既彻底又不会波及用户后来放进目录的文件。
- **为什么自删要外包给临时副本**：Windows 不允许删除正在运行的 exe；复制到 `%TEMP%` 后等父进程
  退出再收尾，才能既删掉原 `Uninstall.exe` 又删掉安装目录。
- **为什么不用 Inno Setup / NSIS**：本机没有安装它们；用 Python + PyInstaller 可以复用现有代码
  和单元测试，全部逻辑可读可改。

## 6. 文件说明

| 文件 | 作用 |
| --- | --- |
| `app.py` | 统一入口：无参数=安装，`--uninstall`=卸载（也是打包内嵌的那个 exe） |
| `app_meta.py` | 应用名、版本、目录、快捷方式名、注册表键、标记文件名 |
| `manifest.py` | 安装清单的读写与安全过滤（拒绝 `..` 和绝对路径） |
| `cleanup.py` | 安装目录安全检查、标记/清单判断、运行残留清单（纯函数） |
| `installer.py` | 安装向导 + `install()` + 静默参数 + 复制自身为 `Uninstall.exe` |
| `uninstaller.py` | 卸载向导 + 清单驱动 `uninstall()` + 临时副本自删 + 残留自检 |
| `registry_ops.py` | 写/读/删 `HKCU` 卸载项，`matches_install()` 核验身份 |
| `shortcuts.py` | 建/删 `.lnk`，`read_shortcut_target()` 读目标路径 |
| `build.py` | 调 PyInstaller，产出 `ChatClient.exe` 与 `ChatClientSetup.exe` |
| `diag.py` | 安装/卸载日志与错误弹窗 |
| `entry_client.py` / `entry_setup.py` / `entry_installer.py` / `entry_uninstaller.py` | 各种入口（客户端 / 二合一 / 仅安装 / 仅卸载） |

## 7. 已知限制

- 打包后的 exe 在受限沙箱里**跑不起来**（进程创建被拦截），所以「双击安装 / 注册表 / 快捷方式 /
  自删」只能在普通桌面环境验证；源码模式的安装与卸载已在仓库内验证（见 `docs/DEVLOG.md`）。
- 首次运行可能触发 SmartScreen「已保护你的电脑」，点「更多信息 → 仍要运行」（无代码签名）。
- 卸载只清理清单里登记的东西，不会动安装目录之外的其它文件。
