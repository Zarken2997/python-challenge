# 开发日志（DEVLOG）

> 项目：Python 挑战课题（Socket 通信 / 数据库 MIS / MNIST 识别）
> 规则见 `AGENTS.md` 第 7 节：**最新条目在最上方**，追加式，不得删改历史条目。
> `目标` / `改动` / `验证` / `下一步` 为必填字段；提交前运行 `python tools/devlog.py check`。

<!-- ENTRIES -->

## 2026-11-23 01:32 · T1 · TCP/UDP 通讯框架与分帧协议
- 目标: 按任务 1 知识点实现 socket 点对点实时通信框架：多客户端广播、私聊、心跳保活、TCP 粘包分帧，并给出 UDP 对照实现
- 改动: `src/task1_socket_chat/protocol.py`、`config.py`、`server.py`、`client.py`、`udp_chat.py`、`__init__.py`、`README.md`（均新增）；`tests/test_protocol.py`、`tests/test_chat_server.py`、`tests/test_udp_chat.py`（新增 50 项）；`requirements/task1.txt`（新增，声明仅标准库）；`AGENTS.md`（题目 docx 路径同步为用户移动后的根目录）
- 验证: `python -m compileall -q src tests` → 退出码 0；`python -m unittest discover -s tests` → `Ran 60 tests in 4.069s` `OK`；端到端 `server --port 9001` + bob(`--send /who`) + alice(`--send 群聊`、`/to bob …`、`/who`) → alice 输出 `在线用户: alice、bob`，bob 输出 `[01:24:01] alice 私聊你: 私聊测试：只给你看`，两进程 exit 0
- 决策: 用 4 字节大端长度前缀 + JSON 分帧而非换行分隔，因为 JSON 正文可能含换行，长度前缀与内容无关；服务端每连接一线程而非 selectors，因为与题目"每个客户端一个线程"一致且便于报告中讲解；客户端收发分线程，因为 `input()` 会阻塞接收、无法实时双向
- 问题: 空白消息 `"   \r\n"` 用 `strip("\r\n")` 后仍非空而被广播，改为 `strip()` 后通过（server.py 群聊与私聊两处）；UDP 中继为对照演示，不保证送达
- 下一步: 写 `docs/reports/task1-design.md`（设计思路/架构/协议表/时序说明）并补运行截图；Windows 控制台中文乱码需先 `chcp 65001`

## 2026-11-23 01:10 · T0 · 建立协作规则、日志机制与忽略规则
- 目标: 落地 AI 协作约束（AGENTS.md）、可机器校验的开发日志机制（docs/DEVLOG.md + tools/devlog.py）与覆盖全部产物的 .gitignore
- 改动: `AGENTS.md`（新增）、`.gitignore`（新增，123 行分节规则）、`.gitattributes`（新增，LF + binary）、`docs/DEVLOG.md`（新增）、`tools/devlog.py`（新增，new/check/stats）、`tests/test_devlog.py`（新增，16 项）
- 验证: `python -m compileall -q tools tests` → 退出码 0；`python -m unittest discover -s tests -v` → Ran 16 tests, OK；`python tools/devlog.py check` → 通过，0 错误
- 决策: 日志用标记 + 工具插入（而非手写），因为顺序与必填字段可被机器校验；单元测试改为内存替换 `read_text`/`write_text`，因为本机沙箱下 Python 子进程可创建子目录却无法写入其下文件（`PermissionError [Errno 13]`）
- 问题: 首次运行 pwsh 时工作区不可写（`SetNamedSecurityInfoW failed (Win32 5)`），已用 diagnose-windows-sandbox-acl 修复，备份与回滚脚本见 F:\codex,ultilize\acl-reports-python-challenge
- 下一步: 从索引移除被误提交的 Word 锁文件 `python挑战课题/~$课题--Python编程--任务1.docx`，然后开始任务 1（socket 点对点通信）
