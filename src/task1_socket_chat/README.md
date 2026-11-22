# 任务 1：基于 socket 的实时通讯框架

本目录是《挑战课题—Python 编程》任务 1（网络程序设计）的实现：用 Python 标准库 `socket` 写一个
**点对点实时双向通信**框架。TCP 版为聊天室（多客户端广播 + 私聊 + 用户列表），TCP/UDP 两套实现
用于对照题目中的协议差异；另提供 tkinter 图形界面（服务器监控台 + 客户端），对应题目加分项。
全程只用标准库，无第三方依赖。

> 题目原文见仓库根目录 `挑战课题--Python编程--任务1.docx`（只读）。
> 课程知识点：socket 概念与 C/S 模型、TCP 三次握手、TCP/UDP 对比、socket 工作流程
> （`socket() → bind() → listen() → accept() → send()/recv() → close()`）。

## 1. 知识点 → 代码对照

| 题目知识点 | 落点 |
| --- | --- |
| IP + 端口、C/S 模型 | `server.py` 监听 `host:port`；`client.py` 主动 `connect()` |
| 服务器为每个客户端开线程 | `ChatServer._accept_loop` 每接受一个连接就起 `threading.Thread(target=self._serve_session)` |
| `socket() → bind() → listen(backlog) → accept()` | `ChatServer.start()`（`socket.SO_REUSEADDR`、`listen(config.backlog)`） |
| `accept()` 返回新 socket + 客户端地址 | `_accept_loop` 中的 `conn, addr = self._sock.accept()`，新 socket 交给 `ClientSession` |
| 客户端 `connect() → send()/recv() → close()` | `ChatClient.connect()` / `_send()` / `_receive_loop()` / `quit()` |
| TCP 字节流会粘包/半包 | `protocol.py` 的 4 字节长度前缀分帧：`send_message` / `read_message` / `FrameDecoder` |
| TCP 可靠、面向连接 | TCP 版：确认、重传、保序由内核负责，应用层只需分帧 |
| UDP 无连接、可能丢包乱序 | `udp_chat.py`：数据报独立发送，靠 hello 注册与 TTL 维护节点表 |
| 一点对多点（加分项） | 服务端广播 `chat`、`join`/`leave` 系统消息；UDP 中继转发 |
| 超时与优雅退出 | 心跳 `ping`/`pong`、`settimeout`、`Ctrl+C` 打印提示后 `shutdown()` |

## 2. 目录与模块

```text
src/task1_socket_chat/
├─ protocol.py   # 消息模型 + 长度前缀分帧（TCP 粘包处理），TCP/UDP 共用
├─ config.py     # 默认端口、长度限制、三个 dataclass 配置（含参数校验）
├─ server.py     # TCP 聊天服务器：多线程、广播、私聊、心跳、重名处理、管理接口
├─ client.py     # TCP 客户端：收发双线程、命令行交互、/who /to /help /quit
├─ udp_chat.py   # UDP 中继 + UDP 节点，演示无连接协议的差异
├─ gui_server.py # tkinter 服务器监控台：在线列表、踢人、公告、终端日志
├─ gui_client.py # tkinter 聊天客户端：聊天记录、在线列表、群聊/私聊
└─ __init__.py   # 对外导出协议层 API
```

## 3. 应用层协议

TCP 上发送的每一帧 = **4 字节大端无符号长度** + **UTF-8 JSON 正文**：

```text
+--------+--------+--------+--------+----------------------------+
| len(4B, big-endian)               | JSON body (len bytes)      |
+--------+--------+--------+--------+----------------------------+
```

- 长度字段只表示正文长度，不含头部；上限 `MAX_PAYLOAD = 64 KiB`。
- 正文是 JSON 对象，字段如下（空字段序列化时省略）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `v` | int | 协议版本，当前 `PROTOCOL_VERSION = 1` |
| `type` | str | 消息类型（见下表），必填且非空 |
| `sender` | str | 发送者昵称 |
| `text` | str | 文本内容 |
| `target` | str\|null | 私聊目标昵称 |
| `ts` | number | Unix 时间戳（秒，浮点） |
| `data` | object | 扩展字段（如 `users`、`event`） |

消息类型：

| type | 方向 | 含义 |
| --- | --- | --- |
| `hello` | 客户端 → 服务端 | 握手，携带昵称 |
| `welcome` | 服务端 → 客户端 | 握手成功，`data.users` 为在线列表，返回实际昵称 |
| `chat` | 双向 | 群聊消息，服务端广播给其他人（不回显给发送者） |
| `private` | 双向 | 私聊，`target` 指定接收者 |
| `who` / `userlist` | 客户端 → 服务端 / 反向 | 查询在线用户 |
| `system` | 服务端 → 客户端 | 加入/离开/服务器关闭通知，`data.event` 标记 `join`/`leave` |
| `error` | 服务端 → 客户端 | 可恢复错误（昵称被占、目标不存在、消息过长等） |
| `ping` / `pong` | 双向 | 心跳保活；客户端收到 `ping` 自动回 `pong` |
| `bye` | 客户端 → 服务端 | 主动离开 |

分帧细节：接收端先 `recv` 到 4 字节头，再按长度循环 `recv` 直到读满正文；
`FrameDecoder` 缓存不完整的尾巴，支持一次 `recv` 读到半帧或多帧（即粘包/拆包）。

UDP 版使用同一个 `Message` 编码为 UTF-8 JSON 放进单个数据报（`MAX_DATAGRAM = 60000`），
不需要长度前缀——数据报本身有边界，但可能丢失、重复、乱序。

## 4. 运行方式

### 4.1 TCP 聊天室（两个终端）

终端 A（先启动服务端，`--port 0` 表示让系统挑一个空闲端口）：

```bash
python -m src.task1_socket_chat.server --port 0
# 聊天服务器已启动 → 127.0.0.1:61523   ← 实际端口以这行输出为准
```

端口也可以写死（如 `--port 9000`），但自动分配能避免「9000 被别的程序占用」这类课堂常见问题。

终端 B、C（各启动一个客户端，端口填上面打印的那个）：

```bash
python -m src.task1_socket_chat.client --port 61523 --name alice
python -m src.task1_socket_chat.client --port 61523 --name bob
```

客户端命令：

```text
直接输入文字        群聊发送
/to <昵称> <消息>   私聊
/who               查看在线用户
/help              帮助
/quit 或 /exit      退出
```

服务端参数：`--host --port --backlog --max-clients --ping-interval --max-missed-pings`。
客户端参数：`--host --port --name --connect-timeout --send --linger --verbose`。
`--send` 可重复，配合 `--linger` 用于无交互演示与回归测试。

### 4.2 无交互演示（一条命令起两个客户端）

```bash
# 终端 A
python -m src.task1_socket_chat.server --port 9001
# 终端 B
python -m src.task1_socket_chat.client --port 9001 --name alice \
  --send "大家好，我是 alice" --send "/to bob 私聊测试：只给你看" --send /who --linger 2
```

### 4.3 UDP 对照演示（三个终端）

```bash
python -m src.task1_socket_chat.udp_chat relay --port 9100
python -m src.task1_socket_chat.udp_chat peer --port 9100 --name alice --linger 8
python -m src.task1_socket_chat.udp_chat peer --port 9100 --name bob --linger 8
```

UDP 版没有 `accept()`：中继收到数据报后按源地址回送/转发，节点靠 `hello` 注册、
`peer_ttl` 秒无消息即从列表移除。可对比说明：UDP 不保证送达，若丢包则对方收不到，
也没有 TCP 的重传与保序，但开销小、天然支持一对多。

### 4.5 图形界面（GUI）

服务器端与客户端是两个独立窗口，各自启动：

```bash
# 窗口 1：服务器监控台（端口默认 0 = 自动分配；点「创建聊天室」即可）
python -m src.task1_socket_chat.gui_server

# 窗口 2、3：客户端（服务器地址可直接粘贴服务器窗口显示的 IP:端口）
python -m src.task1_socket_chat.gui_client --name alice
python -m src.task1_socket_chat.gui_client --name bob
```

端口自动分配的流程：

1. 服务器窗口里「端口（0 = 自动）」保持 0，点 **创建聊天室**；
2. 窗口第二行出现红色地址，例如 **`127.0.0.1:61523`**，旁边有「复制地址」按钮；
3. 客户端窗口把这段地址直接粘贴到「服务器」输入框（会自动拆成主机 + 端口），点「连接」。

服务器监控台能做什么：

- **在线客户端表**：昵称、IP:端口、在线时长，每 1 秒刷新；双击某行直接踢出，也可选中后点「踢出选中」。
- **终端监控**：把服务器进程的 `logging` 输出（监听、加入、离开、广播内容、公告、踢出、错误）
  镜像到窗口里的黑色文本区，与命令行输出一致；可勾选自动滚动、清空、保存为日志文件。
- **发公告**：弹窗输入文字，广播给所有在线客户端。
- 状态栏显示在线人数；点「停止服务」后客户端会收到「服务器关闭」系统消息。

客户端窗口能做什么：

- 聊天记录区按类型着色：自己发的蓝色、他人消息黑色、私聊紫色、系统提示灰色、错误红色。
- 右侧在线用户列表；选中某人后输入框上方显示「发送给：某人（私聊）」，直接回车即私聊；
  点「群聊」取消选中，恢复群发。
- 连接在后台线程执行，界面不会卡死；断线只提示一次，可重新点「连接」。

GUI 只是 `ChatServer` / `ChatClient` 的一层壳：网络与协议逻辑与命令行版完全共用，
所以两者的消息格式、心跳、踢人、公告行为一致。

### 4.5.1 在 PyCharm 里直接运行

每个入口文件顶部都有一段自举代码：被当作脚本直接运行时，它把仓库根目录加入
`sys.path` 并补上包名，所以下面两种方式等价，**都不需要先配 Run Configuration 的 Module name**：

- 在 PyCharm 里右键 `src/task1_socket_chat/gui_server.py` → **Run 'gui_server'**（绿色三角）；
- 或在终端执行 `python src/task1_socket_chat/gui_server.py`。

注意：

- **解释器版本**：代码兼容 **Python 3.7 及以上**（已用 Anaconda 3.7.0 跑通全部 74 项测试），
  PyCharm 里选哪个都行；推荐 `py -0p` 里的 `pythoncore-3.14-64`，Anaconda3 的 3.7 也能跑。
  兼容性靠两点保证：每个模块开头都有 `from __future__ import annotations`（3.7 不会求值
  `list[str]`、`str | None` 这类注解），且没有使用 `dataclass(slots=True)`、`typing.Final`、
  赋值表达式 `:=` 等 3.8+ 才有的语法。
- `python` 如果指向 Windows 的 `WindowsApps\python.exe`（Microsoft Store 占位符），
  双击运行会「什么都没发生」；改用 `py` 启动器，或在「设置 → 应用 → 高级应用设置 → 应用执行别名」
  里关掉它，并把真实解释器目录加入 PATH。
- PyCharm 的 Run Configuration 里不要勾 "Run with Python Console"，否则会停在 `>>>`，
  看起来同样像「没反应」。

### 4.6 局域网实测

把服务端 `/ 中继` 的 `--host` 改为 `0.0.0.0`，客户端 `--host` 改为服务器局域网 IP，
并在服务器防火墙放行对应 TCP/UDP 端口：

```bash
python -m src.task1_socket_chat.server --host 0.0.0.0 --port 9000
python -m src.task1_socket_chat.client --host 192.168.1.20 --port 9000 --name alice
```

## 5. 测试

```bash
python -m compileall -q src tests
python -m unittest discover -s tests -v
```

当前结果：`Ran 74 tests ... OK`，分布在 `test_protocol.py`、`test_chat_server.py`（含管理接口与
客户端回调）、`test_udp_chat.py`、`test_gui.py`（无显示环境自动跳过）、`test_devlog.py`；
全部用回环 socket，端口由系统分配，不依赖外部网络。

## 6. 设计决策

- **长度前缀而非换行分帧**：JSON 正文里可能出现换行，长度前缀与内容无关，解析更稳。
- **线程而非 `selectors`**：课程只讲阻塞 socket 与线程模型，`threading` 与题目"每个客户端一个线程"
  的描述一一对应，代码更容易在报告里讲清；代价是连接数很多时不经济。
- **收发分线程**：客户端主线程读键盘、接收线程读 socket，否则 `input()` 会阻塞收消息，
  无法做到"实时双向"。
- **心跳**：`settimeout(ping_interval)` 既做超时又做保活，连续 `max_missed_pings` 次无响应才断开，
  避免客户端短暂卡顿就被踢。
- **广播排除发送者**：发送者本地回显自己的消息，服务端不再回传，避免重复显示。
- **GUI 复用 CLI 的网络层**：GUI 不做自己的 socket 处理，只给 `ChatClient` 挂 `on_message` /
  `on_disconnect` 回调、给 `ChatServer` 加 `online_users()` / `kick()` / `broadcast_system()`，
  避免两套实现行为漂移；服务器「终端监控」用 `logging.Handler` + 队列把日志搬到 Tk 主线程，
  不在工作线程里碰控件。
- **端口自动分配**：`--port` 默认 0，由系统在 `bind()` 时挑一个空闲端口并回填显示，
  避免课堂演示时端口被占；代价是客户端必须从服务器窗口/终端输出里抄实际端口，
  所以服务器端把地址做成醒目红色文本并提供「复制地址」按钮。
- **入口自举而非改包结构**：在入口模块顶部判断 `__package__`，为空时补 `sys.path` 与包名，
  这样 PyCharm 绿色三角与 `python -m` 都能用，又不用为启动脚本复制一套逻辑。

## 7. 已知限制

- 明文传输、无认证与加密：仅用于课程演示，不能直接用于真实环境。
- 群聊是单房间、无历史消息；服务端不落盘，重启即清空。
- UDP 中继无可靠性保证，丢包即丢消息（这正是与 TCP 对比的演示点）。
- 图形界面需要桌面环境（Windows/macOS/Linux 有显示服务）；无显示环境（如纯 SSH）只能跑命令行版。
- Windows 控制台默认 GBK，若中文乱码请先执行 `chcp 65001` 并设置 `PYTHONIOENCODING=utf-8`。
