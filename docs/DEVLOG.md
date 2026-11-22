# 开发日志（DEVLOG）

> 项目：Python 挑战课题（Socket 通信 / 数据库 MIS / MNIST 识别）
> 规则见 `AGENTS.md` 第 7 节：**最新条目在最上方**，追加式，不得删改历史条目。
> `目标` / `改动` / `验证` / `下一步` 为必填字段；提交前运行 `python tools/devlog.py check`。

<!-- ENTRIES -->

## 2026-11-23 04:05 · T1 · 监控台按房间看消息、人数实时刷新
- 目标: 修三个体验问题——客户端切换房间后人数不刷新；服务器端把所有房间的聊天都打到同一条终端输出里，应该「进入某个房间」才看得到该房消息；房间/申请/踢人等控制面信息应与聊天分开显示，且「看监控」不能改变房间人数
- 改动: `src/task1_socket_chat/server.py`(新增 `on_users_changed` / `on_room_event` 回调，加入/离开时通知人数变化，chat/private 不再写 info 日志而改走回调)、`src/task1_socket_chat/hub.py`(`Room.messages = deque(maxlen=300)` 按房间缓存消息，`ChatHub.on_room_event`、`_record_room_event()`、`room_messages()`，建房间时把 `ChatServer` 的两个回调接到 hub)、`src/task1_socket_chat/gui_server.py`(右侧改为 Notebook 两个标签「房间消息（需进入房间）」「系统日志（房间/申请/踢人）」；新增 `monitor_selected_room()` / `stop_monitor()` / `clear_room_messages()` / `_on_room_event()` / `_drain_room_events()` / `_append_message()`；双击房间改为进入监控，关房改用「关闭选中」按钮)、`src/task1_socket_chat/gui_client.py`(新增 `_auto_refresh_rooms()` 每秒拉一次房间列表)、`tests/test_hub.py`(新增 `RoomMonitorTests` 3 项：两个房间的消息互不串台、`on_room_event` 收到本房 chat、人数变化主动推送且离开后回到 0)、`tests/test_gui.py`(新增监控页测试：未进入房间不显示消息、进入后只显示本房消息、进入监控不改变在线人数、退出后不再追加)、`src/task1_socket_chat/README.md`(GUI 小节改写为两个标签页 + 进入监控流程；测试数改为 97；设计决策补「回调而非解析日志」「推送为主、轮询兜底」)
- 验证: `Anaconda3\python.exe -m compileall -q src tests tools` → exit 0；`Anaconda3\python.exe -m unittest discover -s tests` → `Ran 97 tests` `OK`（连跑 3 次均 OK）；`pythoncore-3.14-64\python.exe -m unittest discover -s tests` → `Ran 97 tests` `OK`（连跑 3 次均 OK）；`logs/_shot_server.py` 截图 `logs/gui-server.png` 显示「正在监控：学习室（127.0.0.1:54795）」且消息区只有学习室的加入/聊天/公告；`logs/gui-server-log.png` 显示系统日志标签只剩建房/申请/公告等控制事件，没有聊天内容；`logs/_shot_client.py` 先加入学习室、再退出加入闲聊室，截图 `logs/gui-client.png` 显示房间表 `学习室 1 人` `闲聊室 1 人`，不再需要手点刷新
- 决策: 让 `ChatServer` 主动回调 hub，而不是让监控台去解析日志字符串，因为日志格式是给人看的、随时会变，回调传的是结构化 `Message`，按房间归类才可靠；进入监控只读内存缓存、不建立任何 socket 连接，所以看监控天然不会改变人数
- 问题: 首轮 `test_room_keeps_its_own_messages` 在 3.14 上偶发失败——原来的等待条件 `len(room_messages) >= 1` 会被「xx 加入了聊天室」这条系统消息提前满足，真正的 chat 还没到；改为等待「具体那句话」出现后稳定通过
- 下一步: 报告与截图仍暂缓；如需要，可给房间消息加搜索/导出，或把每个房间的历史落盘到 `logs/`

## 2026-11-23 03:52 · T1 · 客户端连不上注册中心时自动重试并提示
- 目标: 用户先开客户端、后开服务器时，聊天记录里只看到两行「连接注册中心失败: [WinError 10061]」，不知道要先启动服务器；希望客户端自己等服务器并重试
- 改动: `src/task1_socket_chat/gui_client.py`(新增 `MAX_HUB_RETRIES = 5`、`HUB_RETRY_DELAY_MS = 2000`；`connect_hub(manual=False)` 增加在途判断 `_hub_connecting` 与手动时清零重试计数；失败时打印「请先在服务器窗口点『启动注册中心』，并确认地址是 host:port；2 秒后自动重试（n/5）」并 `root.after` 递归重试；`_on_hub_connected` 清零计数；`disconnect_hub()` 把计数置满以停止重试；「连接注册中心」按钮改为 `manual=True`)、`tests/test_gui.py`(新增 2 项：失败事件后计数为 1 且提示含「启动注册中心」；主动断开后重试计数置满)、`src/task1_socket_chat/README.md`(客户端小节补自动重试说明)
- 验证: `Anaconda3\python.exe -m unittest tests.test_gui -v` → `Ran 11 tests` `OK`；`Anaconda3\python.exe -m unittest discover -s tests` → `Ran 93 tests in 5.194s` `OK`；`pythoncore-3.14-64\python.exe -m unittest discover -s tests` → `Ran 93 tests in 5.159s` `OK`；`logs/_shot_retry.py` 实测：先起客户端（无服务器）→ 截图 `logs/gui-client-retry.png` 显示 10061 错误 + 提示 + 状态「连接失败，1/5 次重试中」；2 秒后再起注册中心并建房 → 截图 `logs/gui-client-recovered.png` 显示「注册中心：已连接 · alice」且房间表出现 `学习室 127.0.0.1:56381`，说明重试确实连上了
- 决策: 用「有限次自动重试 + 明确提示」而不是静默重试或弹窗，因为课堂演示最常见的失误顺序就是先开客户端；静默重试用户会以为卡死，弹窗又会挡住操作，写进聊天区最直观；上限 5 次（约 10 秒）避免服务器一直不开时无限重连
- 问题: 用户截图里同一错误出现两次，原因是自动连接失败后用户又手点了一次「连接注册中心」；现在失败会禁用按钮直到重试结束，避免重复点击叠加请求
- 下一步: 报告与正式截图仍暂缓（用户已说明）

## 2026-11-23 03:49 · T1 · 多聊天室注册中心与开房审批
- 目标: 支持同时存在多个聊天室（每个房间一个端口），监控台统一监控所有房间并可全服发消息；客户端可申请开房、由服务器批准；相同 IP:端口 进入同一房间；客户端启动自动连注册中心
- 改动: `src/task1_socket_chat/hub.py`(新增：`Room`/`RoomRequest`/`ChatHub`/`HubClient`，控制协议 hello/welcome、list_rooms、request_room→room_requested→room_created|room_rejected、rooms 推送)、`src/task1_socket_chat/config.py`(新增 `MAX_ROOMS` 与 `HubConfig`)、`src/task1_socket_chat/gui_server.py`(重写为多聊天室监控台 `MonitorApp`：房间表/开房申请表/跨房间在线用户、建房关闭、批准拒绝、踢人、全服公告、终端日志镜像)、`src/task1_socket_chat/gui_client.py`(启动自动连注册中心、房间列表与加入、申请开房、退出聊天室、`_place_sashes` 摆栏)、`tests/test_hub.py`(新增 17 项)、`tests/test_gui.py`(重写为 9 项多聊天室用例)、`src/task1_socket_chat/README.md`(补 §3.1 控制协议、§4.4 注册中心、改写 §4.5 GUI)、`AGENTS.md:49`(结构树加 `hub.py`)、`tools/devlog.py`(`write_text` 改用 `open(..., newline="\n")`，因为 3.7 的 `Path.write_text` 没有 `newline` 参数)
- 验证: `Anaconda3\python.exe -m compileall -q src tests tools` → exit 0；`Anaconda3\python.exe -m unittest discover -s tests` → `Ran 91 tests in 5.078s` `OK`；`pythoncore-3.14-64\python.exe -m unittest discover -s tests` → `Ran 91 tests in 5.106s` `OK`；`Anaconda3\python.exe src\task1_socket_chat\hub.py --help` 正常打印用法；后台 `python -m src.task1_socket_chat.hub --port 9111` 输出「注册中心已启动 → 127.0.0.1:9111」；截图 `logs/gui-server.png`（房间 学习室/闲聊室、申请 carol「游戏室」、全服公告日志、状态栏「聊天室 2 个 · 在线 2 人」）与 `logs/gui-client.png`（自动连注册中心、房间地址可读、加入学习室后群聊/私聊/「发送给：bob（私聊）」均正常）
- 决策: 一房间一 `ChatServer` 实例、注册中心只存元数据，因为握手/心跳/广播/踢人全部复用既有实现与测试，且「一房间一端口」天然满足「相同 IP:端口 = 同一房间」，代价是多房间多占端口与线程；客户端自动连注册中心而不要求手填地址，因为注册中心端口固定 9000、用户不需要再抄端口
- 问题: `test_hub.py` 首轮 5 项失败——对端关闭时 `recv` 返回 `b""` 而非抛异常（改为读到空串判定关闭）、kick 后注销是异步的（加 `wait_for` 轮询）、申请后先收到 `room_requested` 再收到推送的 `rooms`（用 `read_until` 跳过排队消息）；GUI 首轮截图房间地址与在线用户列被截断（删掉「创建者」「地址」列并显式设置 `sashpos`）
- 下一步: 报告与正式截图仍暂缓（用户已说明）；可考虑统一 `chat.py --protocol tcp|udp` 入口

## 2026-11-23 03:36 · T1 · 服务器端口输入框与实际监听端口同步
- 目标: 用户反馈「服务器控制台上端的端口与实际地址端口不同步」——地址栏显示 `127.0.0.1:50380`，但端口输入框仍是 `0`
- 改动: `src/task1_socket_chat/gui_server.py`(保存 `host_entry`/`port_entry` 引用；`start_server()` 成功后 `self.port_var.set(str(bound_port))` 并把两个输入框置为 DISABLED；`stop_server()` 恢复 NORMAL 且保留端口值，便于用同一地址重启)、`tests/test_gui.py`(断言 `port_var` 等于真实端口、运行中两个输入框 disabled、停止后恢复可编辑且端口保留)、`src/task1_socket_chat/README.md`(端口自动分配流程补「回填 + 锁定 + 停止后恢复」)
- 验证: `Anaconda3\python.exe -m unittest tests.test_gui -v` → `Ran 9 tests` `OK`；`Anaconda3\python.exe -m unittest discover -s tests` → `Ran 74 tests in 5.165s` `OK`（exit 0）；3.14 同样 `Ran 74 tests ... OK`；重跑 `logs/_shot_server.py`（端口 0）→ `saved ... gui-server.png`，截图端口框为 `57900`、状态栏 `监听 127.0.0.1:57900`、地址栏 `127.0.0.1:57900`，三处一致
- 决策: 创建后把输入框改成真实端口而不是继续显示 0，因为界面元素互相矛盾时用户无法判断该抄哪个；运行中锁定输入框，避免「改了端口却没生效」的误解；停止后保留端口，方便原地址重启让客户端不用改配置
- 问题: 无
- 下一步: 无（GUI 功能收尾）

## 2026-11-23 03:26 · T1 · 兼容 Python 3.7（Anaconda），放弃折腾解释器配置
- 目标: 用户说「改不了就不改了，你把代码替换成其他版本能兼容的语句就行」——不再修改 PyCharm 解释器配置，改为让代码本身能在 Anaconda 3.7.0 上运行
- 改动: `src/task1_socket_chat/protocol.py`(去掉 `from typing import Final` 及 3 处 `: Final`；`@dataclass` 去掉 `slots=True`)、`src/task1_socket_chat/config.py`(3 处 `@dataclass(slots=True, frozen=True)` → `@dataclass(frozen=True)`)、`tools/devlog.py`(`pending_required_fields` 里的赋值表达式 `:=` 改成普通循环)、`src/task1_socket_chat/README.md`(§4.5.1 改为「3.7+ 均可」并说明兼容手段)、`AGENTS.md`(§4.1 Python 版本要求改为 3.7+，列出禁用语法)
- 验证: `C:\Users\Lenovo\Anaconda3\python.exe -m compileall -q src tests tools` → exit 0；`Anaconda3\python.exe -m unittest discover -s tests` → `Ran 74 tests in 4.985s` `OK`（exit 0）；`AppData\Local\Python\pythoncore-3.14-64\python.exe -m unittest discover -s tests` → `Ran 74 tests in 4.868s` `OK`（exit 0）；`Anaconda3\python.exe src\task1_socket_chat\gui_server.py --port 0` 启动 5 秒后窗口标题为 `任务1 · 聊天服务器监控台`
- 决策: 用 `from __future__ import annotations` + 去掉 3.8+ 语法来兼容 3.7，而不是加 `typing_extensions` 依赖或写两套代码——注解不影响运行，改动面最小；把 `.idea` 配置恢复原样（Anaconda 3.7），因为代码已能在 3.7 跑，不必再动用户的 IDE 设置
- 问题: 3.7 不支持 `dataclass(slots=True)`、`typing.Final`、`:=`，正是这三个导致 `ImportError: cannot import name 'Final'` 与 `SyntaxError`；此前尝试改 PyCharm 项目 SDK 反而触发 `.port`/`.lock` 单实例锁冲突（`AccessDeniedException`），遂按用户要求回退
- 下一步: 在 PyCharm 里直接用绿色三角运行 `gui_server.py`；若任务 2/3 用到新语法，按 AGENTS.md §4.1 的禁用清单一并遵守

## 2026-11-23 03:12 · T1 · PyCharm 直接运行 + 聊天室端口自动分配
- 目标: 用户要求「把程序启动改为 PyCharm 里运行就可以」，并让服务器创建聊天室时自动分配端口、把实际端口显示出来，客户端能直接用
- 改动: `src/task1_socket_chat/server.py`/`client.py`/`udp_chat.py`/`gui_server.py`/`gui_client.py`(各加入口自举：`__package__` 为空时补 `sys.path` 与包名，绿色三角与 `python -m` 都可用)、`src/task1_socket_chat/gui_server.py`(新增 `display_address`、地址栏与「复制地址」、`start_server()` 返回真实端口、`--port` 默认 0)、`src/task1_socket_chat/server.py`(`--port` 默认 0)、`src/task1_socket_chat/gui_client.py`(新增 `parse_address`，服务器输入框可直接粘贴 `IP:端口`)、`tests/test_gui.py`(新增 `AddressTests` 5 项，服务器窗口测试改用端口 0)、`src/task1_socket_chat/README.md`(补自动端口流程与 4.5.1 PyCharm 运行)、`AGENTS.md`(§9.1 允许端口 0 并要求显示实际端口)
- 验证: `py -m compileall -q src tests` → exit 0；`py -m unittest discover -s tests` → `Ran 74 tests in 4.816s` `OK`（退出码 0）；`py src/task1_socket_chat/gui_server.py` 直接运行 → 枚举窗口得到 `任务1 · 聊天服务器监控台`；截图脚本 `logs/_shot_server.py`（端口 0）→ `实际端口: 61523`，窗口第二行显示红色 `127.0.0.1:61523`，在线表 alice/bob 与队列地址正常；`logs/_shot_client.py` 用「粘贴 127.0.0.1:端口」连接成功，聊天区出现群聊与私聊
- 决策: 用入口自举而非把包改成扁平结构，因为 `python -m` 与 PyCharm 脚本运行要同时成立，扁平化会破坏相对导入的清晰度；客户端让用户粘贴 `IP:端口` 而不是只显示端口，因为自动端口每次不同，抄错一位就连不上
- 问题: 首轮截图发现加宽输入框后工具栏被挤出窗口（「断开」与「已连接」被截断）→ 主机框 18→16、昵称框 12→10，提示文字移到下方；`treeview.cget("state")` 返回 Tcl index 而非字符串 → 测试改用 `instate(["!disabled"])`
- 下一步: 报告与正式截图仍暂缓（用户已说明）；可考虑统一 `chat.py --protocol tcp|udp` 入口

## 2026-11-23 02:48 · T1 · GUI 启动时把窗口提到最前
- 目标: 用户从终端敲 `python -m src.task1_socket_chat.gui_server` 后「光标卡住、不弹窗」，排查并避免窗口被终端压住造成的「没反应」错觉
- 改动: `src/task1_socket_chat/gui_server.py`(新增 `bring_to_front` 并在 main 调用)、`src/task1_socket_chat/gui_client.py`(同上)
- 验证: 枚举窗口句柄的临时脚本（`logs/_check_window.py`，已删）启动两个模块 → `任务1 · 聊天服务器监控台: hwnd=1509654 visible=True`、`任务1 · 聊天客户端: hwnd=3212536 visible=True`；`python -m unittest discover -s tests` → `Ran 69 tests in 4.784s` `OK`
- 决策: 只做「lift + focus_force + 800ms 置顶」而不常驻置顶，因为常驻置顶会挡住用户其它窗口；不改成独立控制台，因为窗口本来就能创建，问题只在层级
- 问题: 进程占住终端是 `mainloop()` 的正常行为，关闭窗口或 Ctrl+C 才返回提示符；若窗口仍不可见，多半在另一显示器或 Alt+Tab 列表里
- 下一步: 报告与截图仍暂缓


## 2026-11-23 02:22 · T1 · 图形界面：服务器监控台与聊天客户端
- 目标: 给任务 1 增加图形界面（对应题目加分项），明确区分服务器端与客户端两个程序；服务器端要能直接监控终端输出、查看在线用户、踢人与发公告
- 改动: `src/task1_socket_chat/gui_server.py`(新增)、`src/task1_socket_chat/gui_client.py`(新增)、`src/task1_socket_chat/server.py`(新增 online_users/user_count/broadcast_system/kick，ClientSession 记录 since)、`src/task1_socket_chat/client.py`(新增 on_message/on_disconnect 回调、echo、request_users)、`tests/test_chat_server.py`(新增 AdminTests 与回调测试)、`tests/test_gui.py`(新增)、`src/task1_socket_chat/README.md`(补 GUI 用法)、`AGENTS.md`(结构树补两个 GUI 文件)
- 验证: `python -m compileall -q src tests` → exit 0；`python -m unittest discover -s tests` → `Ran 69 tests in 4.794s` `OK`；`python -m src.task1_socket_chat.gui_server --port 9106` + 两个 `gui_client`/脚本客户端 → 服务器窗口「在线客户端」显示 alice/bob 与时长，终端监控区依次出现 `监听 127.0.0.1:9106`、`以昵称 alice 加入`、`[广播] alice: 你好，我是 alice`、`[公告] 今晚 22:00 系统维护…`，客户端窗口显示私聊紫色消息与「发送给：bob（私聊）」
- 决策: 用 tkinter 而非 Web/Qt，因为标准库自带、无需安装依赖，课程演示直接 `python -m` 就能开窗；GUI 不做自己的 socket 处理，只给 `ChatClient` 挂回调、给 `ChatServer` 加管理方法，保证 GUI 与命令行行为一致；服务器「终端监控」用 `logging.Handler` + `queue` 把日志搬回 Tk 主线程，避免在工作线程里操作控件
- 问题: 首轮截图发现底部状态栏/输入栏被 PanedWindow 挤出窗口、「群聊」按钮被 Listbox 遮挡、地址列被截断 → 改为先 pack 底部控件与按钮、再让中间面板 expand，地址列加宽到 160；「在线时长」表头仍被截断 → 改名为「时长」
- 下一步: 设计报告与运行截图（用户已说明暂缓）；GUI 后续可加字体缩放与深色主题


## 2026-11-23 02:11 · T1 · 修复心跳 pong 被当作未知消息类型
- 目标: 消除空闲时客户端每隔一个心跳周期收到的 `! 服务器: 未知消息类型: pong`
- 改动: `src/task1_socket_chat/server.py`（`_dispatch` 增加 `pong` 分支，静默接受心跳应答）；`tests/test_chat_server.py`（`test_server_sends_ping_when_idle_and_accepts_pong` 改为收集 pong 之后的全部消息并断言无 error）
- 验证: 修复前 `python -m src.task1_socket_chat.client --port 9021 --linger 4`（server `--ping-interval 1`）连续打印 3 条 `! 服务器: 未知消息类型: pong`；修复后同一命令只输出 `* 已连接 127.0.0.1:9022，昵称 alice` 与 `[02:11:03] alice: hi`，无错误行；`python -m unittest discover -s tests` → `Ran 60 tests in 4.095s`、`OK`，退出码 0
- 决策: 在服务端静默接受 pong，而不是让客户端不发 pong，因为心跳应答本就是协议的一部分，服务端理应认识它；`_chat_loop` 收到任意消息即把 `missed` 清零，pong 因此天然重置超时计数
- 问题: 原测试只断言「pong 之后 /who 仍正常」，未断言期间不出现错误消息，故漏掉该缺陷；已补 `assertNotIn("error", seen)`
- 下一步: 报告与截图按用户要求暂缓；若需要单命令切换 TCP/UDP，再加统一入口的 `--protocol` 参数

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
