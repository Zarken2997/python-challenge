# setupkit —— 客户端安装包与卸载器

把任务 1 的聊天客户端打包成**一个安装包**（`ChatClientSetup.exe`），安装后带**开始菜单快捷方式**
和「设置 → 应用」里的卸载项；卸载器会把快捷方式、程序目录、用户数据目录和注册表项全部清掉。

## 1. 产物

| 文件 | 大小（参考） | 作用 |
| --- | --- | --- |
| `dist/ChatClientSetup.exe` | 约 35 MB | 安装包，双击出现安装向导；内含客户端与卸载器 |
| `dist/ChatClient.exe` | 约 12 MB | 客户端本体（安装包会把它放进安装目录） |
| `dist/Uninstall.exe` | 约 12 MB | 卸载器（安装包会把它放进安装目录，供「设置 → 应用」调用） |

三个 exe 都由 PyInstaller 打成**单文件**（`--onefile --windowed`），不依赖本机 Python。

## 2. 构建

```powershell
cd 'F:\codex,ultilize\python-challenge'

# 1) 建打包专用虚拟环境（只需一次；这一步需要写工作区，必须能创建目录）
py -3 -m venv .venv-pack

# 2) 装 PyInstaller（网络若受限，可参考 logs/_manual_wheels.py 的做法离线安装）
.\.venv-pack\Scripts\python.exe -m pip install pyinstaller==6.22.3

# 3) 构建三个 exe
.\.venv-pack\Scripts\python.exe setupkit\build.py
```

构建过程：`build.py` 先打包客户端与卸载器，把两者放进 `dist/app/`，再用
`--add-data "dist/app;app"` 打进安装包，所以安装包自带卸载器——即使用户先删了程序目录，
也能从「设置 → 应用」重新跑一次卸载。

> PyInstaller 会把 stderr 当错误输出，PowerShell 里可能显示 `[exit code: 1]`；
> 只要日志里有 `Building EXE ... completed successfully` 且 `dist/` 下三个 exe 都在，就算成功。

## 3. 安装

双击 `dist/ChatClientSetup.exe`：

1. 向导里确认安装目录（默认 `%LOCALAPPDATA%\Programs\PythonChatClient`，普通用户可写，不需要管理员）；
2. 勾选是否创建桌面快捷方式 / 开始菜单快捷方式；
3. 点「开始安装」，完成后可直接启动客户端。

静默安装（脚本/批量部署用）：

```powershell
dist\ChatClientSetup.exe --silent --dir "D:\Tools\PythonChatClient" --no-desktop --no-startmenu
```

参数：`--silent` 不弹窗、`--dir` 指定目录、`--no-desktop` / `--no-startmenu` 不建快捷方式、`--run` 安装后启动。

安装做了这些事：

- 复制 `ChatClient.exe`、`Uninstall.exe` 到安装目录；
- 需要时创建桌面/开始菜单快捷方式（`任务1 聊天客户端 客户端.lnk`、`卸载 任务1 聊天客户端.lnk`）；
- 在 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\PythonChatClient` 写入卸载项，
  于是「设置 → 应用 → 已安装的应用」里能看到「任务1 聊天客户端」，点卸载会调用安装目录里的 `Uninstall.exe`。

## 4. 卸载（重点：卸载干净）

三种入口，效果一致：

- 「设置 → 应用 → 任务1 聊天客户端 → 卸载」（走注册表里的 `UninstallString`）；
- 开始菜单里的「卸载 任务1 聊天客户端」；
- 直接运行安装目录里的 `Uninstall.exe`。

静默卸载：

```powershell
"%LOCALAPPDATA%\Programs\PythonChatClient\Uninstall.exe" --silent
```

卸载顺序（`setupkit/uninstaller.py` 的 `uninstall()`）：

1. **结束进程**：`taskkill /F /IM ChatClient.exe`，避免文件占用；
2. **删快捷方式**：桌面、开始菜单程序目录里的 3 个 `.lnk`（客户端 / 开始菜单 / 卸载项），
   并清掉空的开始菜单文件夹；
3. **删用户数据**：`%APPDATA%\PythonChatClient`（客户端配置、日志等）；
   加 `--keep-data` 可保留；
4. **删注册表项**：`HKCU\...\Uninstall\PythonChatClient`；
5. **删安装目录**：如果卸载器自己就在这个目录里（正常安装的情况），先删掉同目录的其它文件，
   再启动一个隐藏的 `cmd` 脚本等待 3 秒后 `rmdir /s /q` 自己——否则 Windows 不允许删除正在运行的程序；
6. **自检**：卸载完成后检查安装目录、数据目录、快捷方式、注册表项是否真的不在了，
   有残留就打印出来并以非 0 退出（方便排查）。

日志写到 `%TEMP%\PythonChatClient-setup.log`（可用环境变量 `PYTHON_CHAT_SETUP_LOG` 改位置），
卸载出问题时先看它。

### 安全护栏：只删「自己装过的目录」

卸载器会先确认目标目录确实是本程序的安装目录，否则**一个文件都不删**、直接退出（退出码 1）：

- 安装时会在安装目录写入标记文件 `install-info.txt`（内容含 `PythonChatClient` 与版本、安装时间）；
- 卸载时 `cleanup.has_install_marker()` 读这个标记；没有标记时，退而要求目录里同时存在
  `ChatClient.exe` 与 `Uninstall.exe` 两个文件（`cleanup.looks_like_install_dir()`）；
- `resolve_install_root()` 不再把「卸载器碰巧所在的目录」直接当安装目录：注册表读不到、或读到的
  路径不像安装目录时，它返回 `None` 并提示用 `--dir` 指定，而不是回退到当前目录。

这条护栏是血的教训换来的：早期版本在 `--dir` 缺省时会回退到「卸载器自身所在目录」，
在 `F:\codex,ultilize` 下双击运行过一次，导致整棵项目目录（含 `.git`）被递归删除。

实测（在被误认的目录里放一个文件再试卸载）：

```text
! 目标目录不像本程序的安装目录（缺少 install-info.txt 或两个 exe），已拒绝卸载: ...\logs\fake-project
  如确认要卸载，请用 --dir 指向真正的安装目录（C:\Users\Lenovo\AppData\Local\Programs\PythonChatClient）。
```

### 可以自己验一遍

```powershell
$dir = "$env:LOCALAPPDATA\Programs\PythonChatClient"

# 1) 安装
dist\ChatClientSetup.exe --silent --dir $dir

# 2) 检查：目录、快捷方式、注册表项
Get-ChildItem $dir
Get-Content "$env:USERPROFILE\Desktop\任务1 聊天客户端 客户端.lnk" -ErrorAction SilentlyContinue | Out-Null
Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PythonChatClient"

# 3) 卸载
& "$dir\Uninstall.exe" --silent
Start-Sleep -Seconds 6      # 自删脚本要等 3 秒

# 4) 复查：三项都应“不存在”
Test-Path $dir
Test-Path "$env:APPDATA\PythonChatClient"
Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PythonChatClient" -ErrorAction SilentlyContinue
```

## 5. 设计说明

- **不需要管理员权限**：装到 `%LOCALAPPDATA%`、只写 `HKCU`，符合课程演示场景，
  也避免 UAC 弹窗；代价是不能装到 `Program Files`（安装目录做了安全检查，
  拒绝盘符根、`C:\Windows`、`C:\Program Files`、`C:\Users\xxx` 这类危险路径）。
- **为什么自带卸载器**：只靠「删目录」会留下快捷方式和注册表项；把 `Uninstall.exe`
  一起装进目录，卸载入口就始终可用，且不需要用户记得原始安装包放在哪。
- **为什么自删要外包给 cmd**：Windows 不允许删除正在运行的 exe，卸载器先删其它文件，
  再让一个隐藏 cmd 等进程退出后删目录，这是常见做法。
- **为什么不用 Inno Setup / NSIS**：本机没有安装它们，课程环境也不一定有；
  用 Python + PyInstaller 可以复用现有代码和单元测试，全部逻辑可读可改。

## 6. 文件说明

| 文件 | 作用 |
| --- | --- |
| `app_meta.py` | 应用名、版本、安装目录/数据目录/快捷方式路径、注册表键，全部集中在这里 |
| `cleanup.py` | 计算「卸载要清哪些东西」、安装目录安全检查（纯函数，好测） |
| `registry_ops.py` | 写/读/删 `HKCU` 卸载项 |
| `shortcuts.py` | 通过 `WScript.Shell` 建/删 `.lnk` |
| `installer.py` | 安装向导 + `install()` + 静默参数 |
| `uninstaller.py` | 卸载向导 + `uninstall()` + 自删脚本 + 残留自检 |
| `build.py` | 调 PyInstaller 产出三个 exe |
| `diag.py` | 安装/卸载日志与错误弹窗 |
| `entry_client.py` / `entry_installer.py` / `entry_uninstaller.py` | PyInstaller 的三个入口 |

## 7. 已知限制

- 打包后的 exe 在受限沙箱里**跑不起来**（进程创建被拦截、注册表写入被拒），
  所以「注册表项 / 快捷方式 / 自删」这三步只能在普通桌面环境验证；
  源码模式下的安装/卸载已在本仓库验证通过（见 `docs/DEVLOG.md`）。
- 首次运行 exe 时 Windows SmartScreen 可能提示「已保护你的电脑」，
  点「更多信息 → 仍要运行」即可（因为没有代码签名证书）。
- 卸载只清理本程序自己的东西；如果用户把客户端装到了自己另外的目录，卸载会按注册表/命令行
  里记录的目录删，不会动其它文件。
