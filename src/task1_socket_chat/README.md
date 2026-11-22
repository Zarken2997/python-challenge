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
├─ hub.py         # 注册中心：多聊天室管理、开房申请审批、全服公告
├─ gui_server.py # tkinter 多聊天室监控台：房间/申请/在线列表、踢人、全服公告、终端日志
├─ gui_client.py # tkinter 聊天客户端：自动连注册中心、选房间、群聊/私聊
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

### 3.1 注册中心（控制端口）协议

`hub.py` 的控制端口管「有哪些聊天室」，它不传聊天内容；聊天内容仍然走各自房间的
`ChatServer`（一房间一端口）。控制端口上的消息类型：

| type | 方向 | 含义 |
| --- | --- | --- |
| `hello` / `welcome` | 双向 | 握手；`welcome.data.rooms` 带房间列表，`version` 为协议版本 |
| `list_rooms` → `rooms` | 客户端 → 服务端 / 反向 | 查询房间列表；`data.rooms` 为快照数组 |
| `request_room` → `room_requested` | 客户端 → 服务端 | 申请开房，`text` 为想要的房间名，`data.request_id` 为回执编号 |
| `room_created` | 服务端 → 客户端 | 申请被批准；`data.room` 是房间快照（含 `host`/`port`） |
| `room_rejected` | 服务端 → 客户端 | 申请被拒绝；`text` 是原因 |
| `rooms` | 服务端 → 客户端 | 房间增删后主动推送，客户端据此刷新列表 |
| `ping` / `pong` | 双向 | 心跳，与聊天室相同 |
| `bye` | 客户端 → 服务端 | 断开控制连接 |

房间本体仍是 `ChatServer`：客户端拿到 `room_created`/`rooms` 里的 `host:port` 后，
用普通的 `hello` 连进那个端口，就进入了这个聊天室——「相同的服务器和端口 = 同一个聊天室」。

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

### 4.4 注册中心（多聊天室）

一个注册中心进程可以同时托管多个聊天室，每个房间各有自己的端口：

```bash
# 注册中心（控制端口 9000，客户端先连这里看房间列表/申请开房）
python -m src.task1_socket_chat.hub --port 9000

# 客户端：连上注册中心 → 选房间 → 拿到该房间的 host:port 后真正加入
python -m src.task1_socket_chat.client --port 61523 --name alice
```

`hub.py` 自己只提供控制协议与命令行入口，日常用图形版更直观（见 4.5）。
它暴露的关键接口：

- `create_room(name, created_by)` / `close_room(room_id)`：服务端直接建房、关房；
- `submit_request(...)` / `approve_request(id)` / `reject_request(id, reason)`：客户端申请、服务器审批；
- `broadcast_all(text)`：全服公告，发到所有房间；
- `rooms_snapshot()` / `online_users()`：给监控台用的房间与在线快照。

### 4.5 图形界面（GUI）

服务器端与客户端是两个独立窗口，各自启动：

```bash
# 窗口 1：多聊天室监控台（控制端口默认 9000，点「启动注册中心」）
python -m src.task1_socket_chat.gui_server

# 窗口 2、3：客户端（启动后自动连注册中心，无需手填地址）
python -m src.task1_socket_chat.gui_client --name alice
python -m src.task1_socket_chat.gui_client --name bob
```

服务器窗口的典型流程：

1. 「控制端口」保持 9000（或填 0 让系统分配），点 **启动注册中心**，右侧出现绿色
   `注册中心 127.0.0.1:9000`；端口框自动回填并锁定；
2. 点 **创建聊天室…** 输入房间名，房间表出现一行，例如 `学习室 127.0.0.1:61523 0 人`；
   地址列就是客户端要连的 `IP:端口`；
3. 客户端申请开房后，「开房申请」表出现「申请人 / 申请房间名 / 时间」，
   选中后点 **批准选中**（建房并通知申请人）或 **拒绝选中**（回原因）；
4. 全服公告输入框 + **发送到所有聊天室**：一次发给所有房间里的所有人；
5. 双击房间行关闭房间（房内会收到「本聊天室已被服务器关闭」），双击在线用户踢出。

服务器监控台能做什么：

- **聊天室表**：名称、地址、人数，房间增删自动刷新；
- **开房申请表**：客户端提交的申请，批准/拒绝都在这里完成；
- **所有聊天室的在线用户**：昵称、所属聊天室、时长，跨房间汇总，双击即踢；
- **终端监控**：把服务器进程的 `logging` 输出（控制端口、建房、申请、加入、广播、公告、踢出）
  镜像到窗口里的黑色文本区，与命令行输出一致；可勾选自动滚动、清空、保存为日志文件；
- 状态栏显示「聊天室 N 个 · 在线 M 人」。

客户端窗口能做什么：

- 启动后**自动连接注册中心**，左上「注册中心」显示连接状态；地址栏也支持粘贴 `IP:端口` 手动重连；
  若服务器还没启动，会提示「请先点『启动注册中心』」并每 2 秒自动重试（最多 5 次），
  服务器后启动也能自动连上，不必关掉客户端重开；
- 左栏房间列表（房间名 / 地址 / 人数）：选中后点 **加入聊天室** 或双击加入；
  「申请开房…」填房间名提交给服务器，批准后弹提示并自动刷新列表；
- 中栏聊天记录按类型着色：自己蓝色、他人黑色、私聊紫色、系统灰色、错误红色；
- 右栏在线用户；选中某人后输入框上方显示「发送给：某人（私聊）」，回车即私聊，点「群聊」恢复群发；
- 未加入房间时发送按钮禁用，避免把消息发丢。

GUI 只是 `ChatHub` / `ChatServer` / `ChatClient` / `HubClient` 的一层壳：网络与协议逻辑与命令行版
完全共用，所以两者的消息格式、心跳、踢人、公告行为一致。

### 4.5.1 在 PyCharm 里直接运行

每个入口文件顶部都有一段自举代码：被当作脚本直接运行时，它把仓库根目录加入
`sys.path` 并补上包名，所以下面两种方式等价，**都不需要先配 Run Configuration 的 Module name**：

- 在 PyCharm 里右键 `src/task1_socket_chat/gui_server.py` → **Run 'gui_server'**（绿色三角）；
- 或在终端执行 `python src/task1_socket_chat/gui_server.py`。

注意：

- **解释器版本**：代码兼容 **Python 3.7 及以上**（已用 Anaconda 3.7.0 跑通全部 91 项测试），
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

当前结果：`Ran 91 tests ... OK`，分布在 `test_protocol.py`、`test_chat_server.py`（含管理接口与
客户端回调）、`test_hub.py`（注册中心：建房/关房/审批/全服公告）、`test_udp_chat.py`、
`test_gui.py`（无显示环境自动跳过）、`test_devlog.py`；全部用回环 socket，端口由系统分配，
不依赖外部网络。

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
- **一房间一 `ChatServer` 实例**：注册中心只记房间元数据，聊天仍由各房间自己的 `ChatServer`
  处理；这样握手、心跳、广播、踢人全部复用既有代码与测试，且「一房间一端口」天然满足
  「相同 IP:端口 = 同一聊天室」的要求，代价是房间多了会多占端口与线程。

## 7. 已知限制

- 明文传输、无认证与加密：仅用于课程演示，不能直接用于真实环境。
- 群聊单个房间内无历史消息；服务端不落盘，注册中心重启后房间与申请全部清空。
- UDP 中继无可靠性保证，丢包即丢消息（这正是与 TCP 对比的演示点）。
- 图形界面需要桌面环境（Windows/macOS/Linux 有显示服务）；无显示环境（如纯 SSH）只能跑命令行版。
- Windows 控制台默认 GBK，若中文乱码请先执行 `chcp 65001` 并设置 `PYTHONIOENCODING=utf-8`。
