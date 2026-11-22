# 任务 1：基于 socket 的实时通讯框架

本目录是《挑战课题—Python 编程》任务 1（网络程序设计）的实现：用 Python 标准库 `socket` 写一个
**点对点实时双向通信**框架。TCP 版为聊天室（多客户端广播 + 私聊 + 用户列表），TCP/UDP 两套实现
用于对照题目中的协议差异。全程只用标准库，无第三方依赖。

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
├─ server.py     # TCP 聊天服务器：多线程、广播、私聊、心跳、重名处理
├─ client.py     # TCP 客户端：收发双线程、命令行交互、/who /to /help /quit
├─ udp_chat.py   # UDP 中继 + UDP 节点，演示无连接协议的差异
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

终端 A（先启动服务端）：

```bash
python -m src.task1_socket_chat.server --port 9000
# 聊天服务器已启动 → 127.0.0.1:9000
```

终端 B、C（各启动一个客户端）：

```bash
python -m src.task1_socket_chat.client --port 9000 --name alice
python -m src.task1_socket_chat.client --port 9000 --name bob
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

### 4.4 局域网实测

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

当前结果：`Ran 60 tests ... OK`（其中协议 30 项、TCP 服务端 13 项、UDP 7 项、日志工具 16 项；
全部用回环 socket，端口由系统分配，不依赖外部网络）。

## 6. 设计决策

- **长度前缀而非换行分帧**：JSON 正文里可能出现换行，长度前缀与内容无关，解析更稳。
- **线程而非 `selectors`**：课程只讲阻塞 socket 与线程模型，`threading` 与题目"每个客户端一个线程"
  的描述一一对应，代码更容易在报告里讲清；代价是连接数很多时不经济。
- **收发分线程**：客户端主线程读键盘、接收线程读 socket，否则 `input()` 会阻塞收消息，
  无法做到"实时双向"。
- **心跳**：`settimeout(ping_interval)` 既做超时又做保活，连续 `max_missed_pings` 次无响应才断开，
  避免客户端短暂卡顿就被踢。
- **广播排除发送者**：发送者本地已回显自己的消息，服务端不再回传，避免重复显示。

## 7. 已知限制

- 明文传输、无认证与加密：仅用于课程演示，不能直接用于真实环境。
- 群聊是单房间、无历史消息；服务端不落盘，重启即清空。
- UDP 中继无可靠性保证，丢包即丢消息（这正是与 TCP 对比的演示点）。
- Windows 控制台默认 GBK，若中文乱码请先执行 `chcp 65001` 并设置 `PYTHONIOENCODING=utf-8`。
