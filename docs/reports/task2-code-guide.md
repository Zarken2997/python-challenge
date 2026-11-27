# 任务2 代码讲解书：聊天服务器运维日志库

> 面向：没接触过 MySQL、对 Python 文件读写不熟的读者。
> 目标：读完能说清"这个系统存了什么、每条数据从哪来、每个函数叫什么、为什么这么写"。
> 配套代码：`src/task2_db_system/`、`src/task1_socket_chat/audit_panel.py`、`src/task1_socket_chat/gui_server.py`。
> 本文所有结论都对应仓库里的真实代码；运行命令见第 8 节。

---

## 目录

1. 先补三个基础概念
2. 系统全貌：数据是怎么进数据库的
3. 文件与模块一览
4. 数据库表设计（逐表讲）
5. 逐模块、逐函数讲解
6. 跟着一条数据走一遍
7. 十个关键设计决策
8. 自己动手运行
9. 常见问题 FAQ
10. 术语表与命名规律

---

## 1. 先补三个基础概念

### 1.1 数据库、表、行、列、主键、外键

把数据库想成**一个 Excel 工作簿**：

| 数据库概念 | Excel 类比 | 本项目例子 |
| --- | --- | --- |
| 数据库（database） | 一个 `.xlsx` 文件 | `data/chat_ops.db` |
| 表（table） | 一张工作表 | `log_event` 运行日志表 |
| 列（column / field） | 表头 | `ts` 时间、`level` 级别 |
| 行（row / record） | 一行数据 | 一条"小明加入了聊天室"的日志 |
| 主键（primary key） | 唯一编号列 | `id INTEGER PRIMARY KEY` |
| 外键（foreign key） | 指向另一张表的编号 | `chat_message.room_id` 指向 `chat_room.id` |

**主键**保证每行有唯一编号，方便"按 id 删除/修改"。
**外键**保证"这条聊天记录的房间号，真的存在于房间表里"，不会出现指向空气的数据。
本项目还额外用 `CHECK` 约束限制取值（比如 `level` 只能是 DEBUG/INFO/WARNING/ERROR/CRITICAL），
用 `UNIQUE` 保证昵称不重复。

### 1.2 SQLite 和 MySQL 有什么区别？

你之前没用过 MySQL，这里直接把两者摆在一起：

| 对比项 | SQLite（本项目用） | MySQL（另一种选择） |
| --- | --- | --- |
| 形态 | 一个 `.db` 文件，程序直接读写 | 一个**服务端进程**，程序通过网络连它 |
| 安装 | Python 自带 `sqlite3` 模块，零安装 | 要装 MySQL Server，建库、建用户、给密码 |
| 连接方式 | `sqlite3.connect("data/chat_ops.db")` | `pymysql.connect(host, port, user, password, database)` |
| 账号密码 | 没有 | 必须有 |
| 并发写入 | 同一时刻只允许一个写者（靠 WAL 缓解） | 多连接并发写（InnoDB 行锁） |
| SQL 占位符 | `?` | `%s` |
| 自增主键写法 | `INTEGER PRIMARY KEY AUTOINCREMENT` | `INT PRIMARY KEY AUTO_INCREMENT` |
| 适用场景 | 单机、小工具、课程作业、嵌入式 | 多人多机访问、网站后端、大数据量 |
| 本作业是否满足要求 | 满足（题目允许 sqlite3 **或** MySQL） | 也满足，但需要额外安装配置 |

**为什么这个作业选 SQLite？**

1. 题目原文写的是"数据库系统使用 sqlite3 或 MySQL"，两者二选一；
2. SQLite 是 Python 标准库，老师和同学拿到代码就能跑，不需要装服务器、配账号；
3. 单文件数据库方便提交和备份，`data/chat_ops.db` 复制走就行；
4. 本系统是"一台服务器 + 一个监控界面"，并发量很小，SQLite 完全够用。

**如果以后要换成 MySQL，需要改哪些地方？**（了解即可，不用现在做）

```python
# 1) 安装驱动：pip install pymysql
# 2) 在 MySQL 里先建库和账号（命令行执行一次）：
#    CREATE DATABASE chat_ops CHARACTER SET utf8mb4;
#    CREATE USER 'chat'@'localhost' IDENTIFIED BY '密码';
#    GRANT ALL ON chat_ops.* TO 'chat'@'localhost';

# 3) 改 db.py 的 connect()：
import pymysql
conn = pymysql.connect(
    host="127.0.0.1", port=3306, user="chat", password="密码",
    database="chat_ops", charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
)

# 4) 把所有 SQL 里的 ? 换成 %s（本项目约 60 处，集中在 repository.py/recorder.py/db.py）
# 5) schema.sql 里 AUTOINCREMENT → AUTO_INCREMENT，TEXT 视情况换 VARCHAR
# 6) 去掉 PRAGMA 语句（WAL、foreign_keys、busy_timeout 是 SQLite 专有）
# 7) 事务改成 conn.begin() / conn.commit() / conn.rollback()
```

本项目的代码结构把数据库操作都收在 `db.py` 和 `repository.py` 里，
就是为了将来换库时只改这两层，界面和统计逻辑不用动。

### 1.3 Python 的文件写入是怎么回事？

你提到"Python 的文件写入不太了解"，这里补最小必要知识。
**注意：数据库文件不是用 `open()` 写的**，要用 `sqlite3` 模块。Python 里"写文件"分三种情况：

**（1）写普通文本文件**

```python
from pathlib import Path

# 写法一：with 语句，离开代码块自动关闭文件（推荐）
with open("data/demo.txt", "w", encoding="utf-8") as f:
    f.write("第一行\n")
    f.write("第二行\n")

# 写法二：pathlib，一行搞定
Path("data/demo.txt").write_text("第一行\n第二行\n", encoding="utf-8")
```

- `"w"` 表示覆盖写，`"a"` 表示追加写；
- `encoding="utf-8"` **必须写**，否则 Windows 默认 GBK，中文容易乱码；
- `with` 会在结束时自动 `close()`，不写 `with` 就要手动关。

**（2）写 CSV（本项目"导出 CSV"用的就是它）**

```python
import csv

with open("data/logs.csv", "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["时间", "级别", "内容"])   # 表头
    writer.writerow(["2026-11-27 10:00:00", "INFO", "服务器启动"])
```

`utf-8-sig` 是"带 BOM 的 UTF-8"，Excel 双击打开中文才不乱码；`newline=""` 防止 Windows 下多出空行。

**（3）写数据库（本项目主要方式）**

```python
import sqlite3

conn = sqlite3.connect("data/chat_ops.db")      # 打开（不存在会自动创建文件）
conn.execute(
    "INSERT INTO log_event (ts, level, message) VALUES (?, ?, ?)",
    ("2026-11-27 10:00:00", "INFO", "服务器启动"),   # ? 依次被这三个值替换
)
conn.commit()                                    # 提交事务，数据才真正落盘
conn.close()
```

关键点：

- SQL 里的 `?` 叫**占位符**，真实值放在第二个参数里，这叫**参数化查询**。
  它同时解决两件事：**防 SQL 注入**、**自动处理引号和转义**。
  绝对不要写成 `"... VALUES ('" + message + "')"` 这种字符串拼接。
- `commit()` 之前的数据只在内存/日志里，断电可能丢；`commit()` 之后才算写完。
- 查询用 `conn.execute("SELECT ...").fetchall()`，每行是 `sqlite3.Row`，可以 `row["列名"]` 取值。

**（4）日志文件的写入（任务1 原来那样）**

`logging` 模块可以同时输出到控制台和文件：

```python
import logging
logging.basicConfig(
    filename="logs/server.log", level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
)
logging.info("服务器启动")
```

本项目的做法更近一步：加一个自定义 `Handler`，把日志**同时写进数据库**（见 5.5 的 `QueueLogHandler`），
所以任务1 的日志一条都不少，还能在数据库里按级别、房间、关键字查询。

---

## 2. 系统全貌：数据是怎么进数据库的

系统有三条数据通路，分别负责不同种类的数据：

```text
                    ┌─────────────────────────────────────────────┐
                    │ 任务1 聊天服务器（server.py / hub.py）        │
                    │  多线程：每个客户端一个线程，每个房间一个端口  │
                    └───────┬──────────────────────┬──────────────┘
                            │                      │
        ① logging 日志      │                      │ ② hub 回调（结构化）
        LOGGER.info(...)    │                      │ room / user / message 对象
                            ▼                      ▼
                 ┌──────────────────┐    ┌──────────────────────┐
                 │ QueueLogHandler  │    │ AuditRecorder        │
                 │ (logging.Handler)│    │ record_room_*()      │
                 └────────┬─────────┘    │ record_operation()   │
                          │ enqueue_log  │ record_room_message()│
                          └──────┬───────┴──────────────────────┘
                                 ▼
                        queue.Queue（内存队列，不阻塞聊天）
                                 │
                                 ▼ 独立线程 task2-audit-writer
                        ┌──────────────────────────┐
                        │ 每 0.5 秒或满 200 条取一批 │
                        │ 一个事务写多张表           │
                        └────────────┬─────────────┘
                                     ▼
                        data/chat_ops.db（SQLite，WAL 模式）
                                     ▲
                                     │ ③ 导入历史日志
                        importer.py 读 logs/*.log 文本 → INSERT
```

三条通路的分工：

| 通路 | 数据来源 | 落到的表 | 特点 |
| --- | --- | --- | --- |
| ① 日志 Handler | `logging` 的每一条记录 | `log_event` | 全量、零侵入，不用改任务1 的日志语句 |
| ② hub 结构化回调 | 房间/用户/消息对象 | `chat_room`、`chat_message`、`user_session`、`operation` | 字段准确，不靠猜文本 |
| ③ 历史日志导入 | 已有的 `.log` 文件 | `log_event` | 补录改造之前的历史数据 |

界面和命令行只读数据库，不直接碰聊天服务：

```text
任务1 监控台（gui_server.py）
   └── 第 3 个标签页 AuditPanel ──读──┐
命令行 cli.py ───────────────────────┼──→ data/chat_ops.db
统计 stats.py ───────────────────────┘
```

---

## 3. 文件与模块一览

```text
src/task2_db_system/
├─ __init__.py      包入口：导出 APP_NAME / APP_VERSION / DEFAULT_DB_PATH / __version__
├─ config.py        所有常量：路径、分页大小、枚举、中英文标签
├─ schema.sql       建表语句（8 张业务表 + 1 张元数据表 + 索引）
├─ db.py            连接、PRAGMA、建库、事务、表行数
├─ repository.py    所有 SQL 查询与增删改（界面/CLI 的唯一数据入口）
├─ stats.py         统计数字（概览、级别分布、趋势、Top 房间）
├─ recorder.py      日志/操作/消息 → 数据库（写线程 + 队列 + 批量事务）
├─ importer.py      历史 .log 文件导入
├─ seed.py          演示数据（幂等）
└─ cli.py           字符界面菜单 + --demo / --stats

src/task1_socket_chat/
├─ audit_panel.py   监控台里的"数据库审计（任务2）"标签页
├─ gui_server.py    启动/停止时挂载记录器、把管理员操作写进审计表
└─ hub.py           新增三个房间回调，供记录器使用

tests/
├─ test_task2_db.py       schema/筛选/写入/统计 13 项
├─ test_task2_recorder.py 记录器 6 项
└─ test_task2_cli.py      导入器 + CLI 冒烟 7 项
```

每个模块只做一件事：

- **config** 只放常量和枚举校验，不写业务；
- **db** 只管"怎么连、怎么建、怎么提交事务"；
- **repository** 只管"SQL 怎么写"，不碰 Tk 界面；
- **recorder** 只管"把事件排队并批量落库"；
- **audit_panel / cli** 只管"怎么展示和接收输入"。

这种分层的好处：换界面（GUI ↔ 命令行）不用改 SQL，改表结构只动 repository。

---

## 4. 数据库表设计（逐表讲）

数据库文件默认在 `data/chat_ops.db`。建表语句在 `src/task2_db_system/schema.sql`。

### 4.1 `schema_meta`：版本元数据

| 列 | 类型 | 说明 |
| --- | --- | --- |
| key | TEXT PRIMARY KEY | 配置名，目前只有 `schema_version` |
| value | TEXT NOT NULL | 版本号，当前为 `"1"` |

作用：以后表结构升级时，程序能知道这个库是第几版，方便写迁移脚本。

### 4.2 `server_instance`：一次服务器运行

| 列 | 类型/约束 | 说明 |
| --- | --- | --- |
| id | INTEGER PK AUTOINCREMENT | 运行实例编号 |
| name | TEXT NOT NULL | 实例名，界面传"聊天服务器" |
| host / port | TEXT / INTEGER（0~65535） | 监听地址与端口 |
| protocol | TEXT，CHECK IN ('tcp','udp') | 协议 |
| started_at | TEXT NOT NULL | 启动时间 |
| stopped_at | TEXT | 停止时间，运行时为 NULL |
| note | TEXT | 备注 |

一次"启动注册中心 → 停止"就是一行。停止时只补 `stopped_at`，不新开行，
这样统计"本次运行产生了多少日志"很直接。

### 4.3 `chat_room`：聊天室

| 列 | 类型/约束 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | 数据库房间编号 |
| instance_id | FK → server_instance，级联删除 | 属于哪次运行 |
| hub_room_id | INTEGER NOT NULL | 任务1 内存里的房间号 |
| name | TEXT NOT NULL | 房间名 |
| host / port | TEXT / INTEGER | 该房间自己的监听地址（每个房间一个端口） |
| created_by | TEXT | 创建者 |
| created_at / closed_at | TEXT | 创建/关闭时间 |
| close_reason | TEXT | 关闭原因 |

`UNIQUE(instance_id, hub_room_id)` 保证同一次运行里房间号不重复，
所以重复收到"创建房间"事件时可以安全地 `SELECT` 后更新，而不是插两行。

### 4.4 `chat_user`：出现过的昵称

| 列 | 类型/约束 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | 用户编号 |
| nickname | TEXT NOT NULL UNIQUE | 昵称（全局唯一） |
| first_seen / last_seen | TEXT | 第一次/最近一次出现 |
| note | TEXT | 人工备注——这是 MIS 的"改"功能之一 |

### 4.5 `user_session`：一次进入房间

| 列 | 类型/约束 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | 会话编号 |
| instance_id / room_id / user_id | 外键 | 哪次运行、哪个房间、谁 |
| addr | TEXT | 客户端地址 `IP:端口` |
| joined_at | TEXT NOT NULL | 进入时间 |
| left_at | TEXT | 离开时间，在房间里时为 NULL |
| leave_reason | TEXT | 离开原因（正常离开/被踢/断线） |

`CHECK (left_at IS NULL OR left_at >= joined_at)` 防止出现"离开早于进入"的脏数据。

### 4.6 `log_event`：运行日志（核心表）

| 列 | 类型/约束 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | 日志编号 |
| instance_id | FK，可为 NULL | 历史导入的日志不属于某次运行 |
| ts | TEXT NOT NULL | 时间 `YYYY-MM-DD HH:MM:SS` |
| level | TEXT，CHECK 五个级别 | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| logger | TEXT | 日志来源，如 `task1.server`、`task1.hub` |
| event_type | TEXT | 中文归类：加入/离开/聊天/踢出/公告/错误… |
| room_name | TEXT | 相关房间 |
| actor | TEXT | 相关用户 |
| message | TEXT NOT NULL | 日志正文 |
| detail | TEXT | 异常堆栈等附加信息 |
| thread | TEXT | 线程名 |

`ts` 用字符串存，是因为格式固定，字符串比较=时间比较，还方便 `substr(ts,1,10)` 按天分组。

### 4.7 `operation`：操作审计

| 列 | 类型/约束 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | 操作编号 |
| instance_id | FK，可为 NULL | 全局操作（导入/清理）可为空 |
| ts | TEXT | 操作时间 |
| action | TEXT，CHECK 15 种 | START/STOP/ROOM_CREATE/KICK/BROADCAST… |
| actor | TEXT | 操作者（"管理员"/"服务器"） |
| target | TEXT | 操作对象（被踢的人、房间名、全服） |
| room_name | TEXT | 相关房间 |
| result | TEXT，CHECK ('ok','failed') | 成功/失败 |
| detail | TEXT | 说明，如踢人原因、公告内容前 100 字 |

这张表回答"谁在什么时候对什么做了什么、成没成功"。

### 4.8 `chat_message`：聊天正文

| 列 | 类型/约束 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | 消息编号 |
| instance_id | FK NOT NULL | 哪次运行 |
| room_id | FK，可为 NULL，ON DELETE SET NULL | 房间被删后消息仍保留，只是不再关联房间 |
| ts | TEXT | 消息时间 |
| kind | TEXT，CHECK ('chat','private','system') | 群聊/私聊/系统 |
| sender | TEXT | 发送者 |
| recipient | TEXT | 私聊接收者 |
| content | TEXT | 正文，最长 4000 字（`CONTENT_LIMIT`） |
| content_len | INTEGER，CHECK >= 0 | 正文字符数，方便按长度排序/筛选 |

聊天单独一张表，不和运行日志混在一起：
查日志不会被大段正文拖慢，清理日志也不会误删聊天记录。

### 4.9 `incident`：事故/处置记录（增删改主要落这里）

| 列 | 类型/约束 | 说明 |
| --- | --- | --- |
| id | INTEGER PK | 事故编号 |
| instance_id | FK，SET NULL | 可为空 |
| title | TEXT NOT NULL | 标题，必填 |
| severity | TEXT CHECK ('低','中','高') | 严重级别 |
| status | TEXT CHECK ('待处理','处理中','已关闭') | 处理状态 |
| assignee | TEXT | 负责人 |
| description | TEXT | 详细描述 |
| log_event_id | FK → log_event，SET NULL | 可关联某条日志 |
| operation_id | FK → operation，SET NULL | 可关联某次操作 |
| created_at / updated_at | TEXT | 创建/更新时间 |
| resolved_at | TEXT | 关闭时间，非"已关闭"时为 NULL |

### 4.10 索引与 WAL

- 索引：`ts`、`level`、`event_type`、`action`、`room_id`、`sender`、`status` 等都建了索引，
  因为界面最常按时间排序、按级别筛选。
- `PRAGMA journal_mode=WAL`：写日志时读界面不会被阻塞（读写可以同时进行），
  数据库目录下会多出 `chat_ops.db-wal`、`chat_ops.db-shm` 两个临时文件，正常现象。
- `PRAGMA foreign_keys=ON`：SQLite 默认**不**强制外键，必须每个连接手动打开，
  所以所有连接都统一走 `db.connect()`。

---

## 5. 逐模块、逐函数讲解

> 函数名都写成 `模块.函数名`。表格里的"名字拆解"解释英文单词，方便记忆。

### 5.1 `config.py`：集中配置

文件里全是常量，没有复杂逻辑。常量就是"全局固定的值"，改一处就全项目生效。

| 名字 | 值 | 含义 |
| --- | --- | --- |
| `REPO_ROOT` | 仓库根目录 | 由 `Path(__file__)` 往上两级得到 |
| `PACKAGE_DIR` | 本包目录 | `src/task2_db_system` |
| `SCHEMA_PATH` | `.../schema.sql` | 建表脚本位置 |
| `DEFAULT_DB_PATH` | `data/chat_ops.db` | 默认数据库文件 |
| `APP_NAME` / `APP_VERSION` | 字符串 | 界面标题用 |
| `PAGE_SIZE` | 50 | 默认每页条数 |
| `PAGE_SIZES` | (20, 50, 100, 200) | 界面"每页"下拉选项 |
| `MAX_EXPORT_ROWS` | 20000 | 单次导出上限 |
| `CONTENT_LIMIT` | 4000 | 正文/日志最大保存长度 |
| `LOG_LEVELS` | DEBUG…CRITICAL | 日志级别枚举 |
| `EVENT_TYPES` | 启动/停止/加入/… | 日志事件中文枚举 |
| `MESSAGE_KINDS` | chat/private/system | 消息类型枚举 |
| `ACTION_LABELS` | 英文动作 → 中文 | `KICK → 踢出用户` |
| `ACTIONS` | ACTION_LABELS 的键 | 全部动作枚举 |
| `RESULT_LABELS` | ok/failed → 成功/失败 | |
| `SEVERITIES` / `STATUSES` | 低中高 / 待处理… | 事故枚举 |
| `PROTOCOLS` | ('tcp','udp') | |

**`normalize_choice(value, allowed, field) -> str`**

- 名字拆解：normalize = 规范化，choice = 选项，allowed = 允许的集合。
- 作用：校验一个下拉框/命令行输入是否在允许值里。
- 规则：空串表示"不筛选"，直接返回 `""`；否则必须精确命中，不然抛 `ValueError`，
  错误信息带字段名和可选值。
- 为什么不用数据库的 `CHECK` 代替：`CHECK` 只能拦住写库，拦不住界面里一次错误的筛选；
  两层校验各管一段。

### 5.2 `db.py`：连接与建库（7 个函数）

| 函数 | 名字拆解 | 作用 |
| --- | --- | --- |
| `now_text(moment=None)` | now=现在，text=文本 | 把时间戳转成 `YYYY-MM-DD HH:MM:SS`；不传参数取当前时间 |
| `connect(path=None, readonly=False)` | connect=连接 | 打开数据库并统一设置 PRAGMA；`readonly=True` 走只读 URI |
| `init_db(path=None)` | init=初始化，db=数据库 | 执行 `schema.sql` 建表，重复执行安全；返回数据库路径 |
| `is_initialized(conn)` | initialized=已初始化 | 检查 `log_event` 表是否存在，防止把空文件当成本系统的库 |
| `open_db(path=None, create=True)` | open=打开 | 业务入口：不存在就先建库，再校验表结构，最后返回连接 |
| `transaction(conn)` | transaction=事务 | 上下文管理器：正常 `commit`，异常 `rollback` |
| `table_counts(conn)` | counts=计数 | 返回 8 张表各有多少行，界面概览/测试断言用 |

逐个展开：

**`now_text(moment=None)`**
`datetime.fromtimestamp(float(moment)).strftime(TIME_FORMAT)`。
所有写入时间都走它，保证格式一致。

**`connect(path=None, readonly=False)`**
- 只读模式：`db_path.resolve().as_uri() + "?mode=ro"`，用 `uri=True` 打开，
  这样即使误写 `INSERT` 也会被数据库拒绝，适合"只查看"的场景；
- 写模式：自动 `mkdir(parents=True, exist_ok=True)` 建父目录；
- `conn.row_factory = sqlite3.Row`：让查询结果可以按列名取值（`row["level"]`）；
- `timeout=5.0` + `PRAGMA busy_timeout=5000`：写锁冲突时最多等 5 秒，而不是立刻报
  `database is locked`。

**`init_db(path=None)`**
读 `schema.sql` 全文交给 `conn.executescript()`，再写一条 `schema_version`。
因为脚本里全是 `CREATE TABLE IF NOT EXISTS`，所以跑一百次也不会报错（幂等）。

**`is_initialized(conn)`**
查 `sqlite_master` 系统表里有没有 `log_event`。为什么需要它：
用户可能误把一个空文件或别的 `.db` 当成本系统的库打开，这时应该明确报错，而不是后续 SQL 全部失败。

**`open_db(path=None, create=True)`**
组合拳：不存在 → `init_db`；连上后 → `is_initialized`；不通过就关闭连接并抛
`RuntimeError("不是本系统的数据库（缺少表结构）: ...")`。

**`transaction(conn)`**
```python
try:
    yield conn
    conn.commit()
except Exception:
    conn.rollback()
    raise
```
批量写 200 条日志时，一个事务比 200 次自动提交快一个数量级，而且保证"要么全写、要么全不写"。

**`table_counts(conn)`**
对 8 张固定表循环 `SELECT COUNT(*)`。表名写死在元组里，不拼用户输入，所以这里的 f-string 是安全的。

### 5.3 `repository.py`：数据访问层（核心）

> 记住一句话：**界面和命令行都不许自己写 SQL，全部调用这里的方法。**
> 这样排序白名单、LIKE 转义、分页边界只实现一次，也不会漏掉参数化。

#### 5.3.1 四个模块级辅助函数

**`_like(value) -> str`**

- 名字：like 指 SQL 的 `LIKE` 模糊匹配。
- 作用：把用户输入变成 `%关键字%`，并转义 `%`、`_`、`\`。
- 例子：搜索 `100%` 时，用户想找字面量"100%"；如果不转义，`%` 会变成通配符，
  结果会匹配到"100 分"等无关内容。
- SQL 里配套写 `LIKE ? ESCAPE '\'`。

**`_bound_start(value)` / `_bound_end(value)`**

- 名字：bound=边界，start/end=开始/结束。
- 作用：用户只输入日期 `2026-11-27` 时，开始补成 `2026-11-27 00:00:00`，
  结束补成 `2026-11-27 23:59:59`，保证"截止到某天"包含当天。
- 少于 10 个字符会抛 `ValueError`，提示格式。

**`_order(sort, desc, allowed)`**

- 名字：order 指 SQL 的 `ORDER BY`，desc=降序。
- 作用：把前端传来的列名映射成真实列名，生成 `ORDER BY ts DESC`。
- **白名单**：`allowed` 里没有的字段直接抛错。这是防 SQL 注入的关键，
  因为 `ORDER BY` 不能用 `?` 占位，只能靠白名单。

**`_page_args(page, page_size)`**

- 返回 `(page, page_size, offset)`；页码最小 1，每页 1~500 条，
  `offset = (page-1) * page_size`。

**`as_dict(row)`**

- `sqlite3.Row` 转普通 `dict`；`None` 原样返回，方便调用方统一处理"查不到"。

#### 5.3.2 `class Repository` 的方法

**运行日志**

| 方法 | 作用 |
| --- | --- |
| `list_logs(keyword="", level="", event_type="", room="", actor="", start="", end="", instance_id=0, sort="ts", desc=True, page=1, page_size=PAGE_SIZE)` | 多条件筛选 + 排序 + 分页，返回 `{rows,total,page,page_size,pages}` |
| `get_log(log_id)` | 按主键取一条日志（详情弹窗用） |
| `recent_errors(limit=10)` | 最近 N 条 ERROR/CRITICAL，给"最近错误"面板 |

`list_logs` 的筛选逻辑：

1. 先校验 `level`、`event_type` 是合法枚举（`normalize_choice`）；
2. 关键字同时匹配 `message`、`detail`、`actor`、`room_name` 四列（`OR`）；
3. 房间、人员各自 `LIKE`；
4. 时间用 `ts >= ?` / `ts <= ?`；
5. 交给内部方法 `_paged(...)` 统一做 COUNT + LIMIT/OFFSET。

**聊天记录**

`list_messages(keyword="", kind="", room="", sender="", start="", end="", sort="ts", desc=True, page=1, page_size=PAGE_SIZE)`

- 和日志类似，但需要 `LEFT JOIN chat_room` 才能显示房间名；
- `LEFT JOIN` 保证房间被删后消息还能查出来（`room_id` 为 NULL）；
- 返回 `{rows,total,page,page_size}`（注意：**没有** `pages` 键，调用方自己算）。

**操作审计**

`list_operations(keyword="", action="", actor="", result="", start="", end="", sort="ts", desc=True, page=1, page_size=PAGE_SIZE)`

- 关键字匹配 `target`、`detail`、`room_name`；
- `action`、`result` 都走枚举校验。

`insert_operation(action, actor="服务器", target="", room_name="", result="ok", detail="", instance_id=None, ts="") -> int`

- 给"导入日志""清理日志"这类全局操作留痕；
- 返回新行 id；`instance_id=None` 表示不属于某次运行。

**事故记录**

| 方法 | 作用 |
| --- | --- |
| `list_incidents(keyword="", status="", severity="", sort="updated_at", desc=True, page=1, page_size=PAGE_SIZE)` | 关键字匹配标题/描述/负责人 |
| `get_incident(incident_id)` | 取一条，编辑前回填表单 |
| `create_incident(title, severity="中", status="待处理", assignee="", description="", log_event_id=None, instance_id=None) -> int` | 新建；标题必填、关联日志必须存在；状态直接是"已关闭"时自动填 `resolved_at` |
| `update_incident(incident_id, **fields) -> bool` | 只允许改 `title/severity/status/assignee/description/log_event_id`；状态改成"已关闭"时补 `resolved_at`，改回其它状态时清空 |
| `delete_incident(incident_id) -> bool` | 删除，返回是否真的删到 |

`update_incident` 的 `**fields` 是"关键字参数收集"，调用时可以
`update_incident(3, status="已关闭", assignee="小赵")`，
不认识的字段会抛 `ValueError`，防止写错列名却静默成功。

**房间 / 用户 / 会话 / 实例**

| 方法 | 作用 |
| --- | --- |
| `list_rooms(instance_id=0, keyword="")` | 房间列表，带会话数、消息数子查询 |
| `list_users(keyword="")` | 用户列表，带会话数、发言数 |
| `list_sessions(active_only=True, limit=300)` | 会话列表，默认只看"还没离开" |
| `update_user_note(nickname, note) -> bool` | 改用户备注（MIS 的"改"） |
| `list_instances(limit=50)` | 运行实例历史 |
| `ensure_instance(name, host, port, protocol="tcp", started_at="") -> int` | 记录一次启动，返回实例 id |
| `stop_instance(instance_id, stopped_at="")` | 补停止时间（只更新 `stopped_at IS NULL` 的行） |
| `ensure_room(instance_id, hub_room_id, name, host, port, created_by, created_at="") -> int` | 幂等：房间已存在就更新地址，否则插入 |
| `close_room(instance_id, hub_room_id, reason="") -> bool` | 补关闭时间与原因 |
| `ensure_user(nickname, seen_at="") -> int` | 幂等：用户已存在只更新 `last_seen` |
| `open_session(instance_id, room_id, user_id, addr="", joined_at="") -> int` | 插入一条进入记录 |
| `close_session(session_id, reason="") -> bool` | 按会话 id 关闭 |
| `close_user_sessions(user_id, room_id, reason="") -> int` | 兜底：把某人在某房间所有未关闭会话都关掉（断线时用） |

"ensure" 的意思是"确保存在"：有就复用，没有就创建。
recorder 的写线程每秒可能收到很多条消息，用 ensure 可以避免重复插入。

**日志清理与导出**

| 方法 | 作用 |
| --- | --- |
| `purge_logs(before) -> int` | 删除 `before` 之前的日志；**拒绝只给日期**，必须带时分秒，防误删一整天 |
| `export_logs(**filters)` | 复用 `list_logs` 的筛选，一次取最多 `MAX_EXPORT_ROWS` 条 |

**内部方法**

`_paged(table, select_sql, clauses, params, order_sql, page, page_size)`

- 名字：paged=分页的。
- 先 `SELECT COUNT(*)` 算总数，再 `SELECT ... ORDER BY ... LIMIT ? OFFSET ?` 取当前页；
- 返回 `{rows, total, page, page_size, pages}`，其中
  `pages = max(1, (total + page_size - 1) // page_size)`（向上取整）。

### 5.4 `stats.py`：统计

这些函数只读，给界面概览和命令行 `--stats` 用。

| 函数 | 名字拆解 | 返回 |
| --- | --- | --- |
| `_scope(instance_id=0)` | scope=范围 | 生成 `WHERE instance_id = ?` 片段，0 表示全部实例 |
| `_scalar(conn, sql, params=())` | scalar=单个数值 | 执行一条聚合 SQL，返回整数（空结果返回 0） |
| `overview(conn, instance_id=0)` | overview=概览 | 日志总数/错误数/警告数/首末时间、消息数、操作数、未关闭事故、房间数、用户数、在线会话数 |
| `level_counts(conn, instance_id=0)` | level=级别 | 每个级别的条数 |
| `event_type_counts(conn, limit=10, instance_id=0)` | event_type=事件类型 | 事件类型 Top N |
| `action_counts(conn, limit=10)` | action=动作 | 操作动作 Top N（成功+失败合并） |
| `hourly_counts(conn, hours=24, instance_id=0, now="")` | hourly=按小时 | 最近 N 小时每小时条数，**没数据的小时补 0** |
| `top_rooms(conn, limit=8)` | top=排行 | 消息最多的房间 |
| `incident_summary(conn)` | summary=汇总 | `{by_status: [...], by_severity: [...]}` |
| `daily_counts(conn, days=7, instance_id=0)` | daily=按天 | 最近 N 天每天条数（升序） |

`overview` 里有个小技巧：一次查询同时算总数、错误数、警告数、最早/最晚时间：

```sql
SELECT COUNT(*) AS total,
       SUM(CASE WHEN level = 'ERROR' THEN 1 ELSE 0 END) AS errors,
       SUM(CASE WHEN level = 'WARNING' THEN 1 ELSE 0 END) AS warnings,
       MIN(ts), MAX(ts)
FROM log_event
```

`hourly_counts` 为什么要在 Python 里补 0：SQL 只能返回"有数据的小时"，
画柱状图时中间缺几个小时会看起来断开，所以先在 Python 里生成完整的小时序列，
再用查到的字典填充。

### 5.5 `recorder.py`：把事件写进数据库（写线程）

这是任务2 与任务1 之间最重要的桥。

#### 5.5.1 分类函数

**`_text_time(value)`**

把 `time.time()` 的浮点数（如 `1764201234.5`）或已格式化的字符串统一成 19 位时间文本。
任务1 的 `Room.created_at` 是浮点数，所以需要它。

**`classify_text(message, levelno=logging.INFO) -> {"event_type","actor"}`**

- 名字：classify=分类，text=文本。
- 作用：按 `EVENT_HINTS` 里的关键字猜事件类型，比如文本含"加入了聊天室"→ `加入`，
  含 `[踢出]` → `踢出`；猜不到时，ERROR 及以上归"错误"，其余归"其他"。
- 同时用三个正则提取人名：
  - `_JOIN_RE`：`以昵称 小明 加入` → 小明
  - `_LEAVE_RE`：`小明 离开聊天室` → 小明
  - `_KICK_RE`：`[踢出] 小明（管理员...` → 小明

**`classify_log(record) -> {"event_type","actor","room_name"}`**

- 名字：record 是 `logging.LogRecord`（一条日志记录）。
- 优先读任务1 挂在记录上的 `event_type`/`actor`/`room_name` 属性；
- 没有才用 `classify_text` 猜。这样结构化字段优先，猜关键字只是兜底。

#### 5.5.2 `QueueLogHandler`

`class QueueLogHandler(logging.Handler)`

- 名字：queue=队列，handler=处理器。
- `logging` 的机制是"日志 → 若干 Handler"。这个 Handler 不打印、不写文件，
  只把记录塞进 `AuditRecorder` 的队列，所以对聊天线程几乎零开销。
- `emit(record)`：真正被 logging 调用的方法。
  - 先判断 `record.name.startswith("task2")` 就直接返回——**防止递归**：
    记录器自己打日志 → 又被自己记录 → 又打日志，会无限循环；
  - 其它情况调 `self.recorder.enqueue_log(record)`；
  - 包在 `try/except` 里并调 `handleError`，因为日志处理器抛异常会污染业务代码。

#### 5.5.3 `class AuditRecorder`

构造函数：

```python
AuditRecorder(
    db_path=None,              # 数据库路径，默认 data/chat_ops.db
    instance_name="聊天服务器",  # 实例名
    host="", port=0,           # 监听地址
    protocol="tcp",
    flush_interval=0.5,        # 最多等 0.5 秒就落库一次
    batch_size=200,            # 或攒够 200 条就落库
)
```

内部状态：

| 属性 | 含义 |
| --- | --- |
| `_queue` | `queue.Queue`，所有待写数据先排队 |
| `_stop` / `_ready` | 两个 `threading.Event`，控制线程退出、通知"写线程已就绪" |
| `_thread` | 写线程对象 |
| `_handler` | 挂在 `task1` logger 上的 Handler |
| `_conn` | 写线程独占的数据库连接（SQLite 连接不能跨线程用） |
| `_rooms` / `_users` / `_sessions` | 内存缓存：hub 房间号 → 数据库 id 等 |
| `written` / `dropped` / `errors` | 计数器，`stats()` 返回给界面 |

**生命周期方法**

| 方法 | 作用 |
| --- | --- |
| `start() -> bool` | 起写线程，最多等 5 秒直到建库/连接完成；成功返回 `enabled` |
| `stop(timeout=5.0)` | 先摘 Handler，再通知线程退出，join，最后清空缓存 |
| `attach_logging(logger_name="task1", level=logging.DEBUG)` | 把 Handler 挂到 `task1` 这棵 logger 树上，幂等 |
| `detach_logging()` | 从挂载时记录的 logger 上摘掉 Handler |
| `stats() -> dict` | 返回 `enabled/db/instance_id/pending/written/dropped/errors` |

**`start()` 的细节**：`_thread.start()` 后 `_ready.wait(5.0)`。
写线程内部如果建库失败，会置 `enabled=False` 并 `_ready.set()`，
所以 `start()` 能立刻知道"库不可用"，而不用等超时。

**对外写入方法**（都只入队，不做磁盘 I/O）

| 方法 | 写入的表/时机 |
| --- | --- |
| `enqueue_log(record)` | 日志 → `log_event`；把时间、级别、logger、分类、消息、异常堆栈、线程名打包 |
| `record_operation(action, actor="服务器", target="", room_name="", result="ok", detail="")` | 操作 → `operation` |
| `record_room_message(room, message)` | 消息 → `chat_message`；`message.type` 决定 kind，`message.data["event"]` 用于区分 join/leave |
| `record_room_created(room)` | 入队一个 `room_created` 事件，等 `_flush` 时 ensure 房间 |
| `record_room_closed(room, reason="")` | 入队 `room_closed`，flush 时补关闭时间 |
| `record_server_started()` | 写一条 `START` 操作，detail 是监听地址 |
| `record_server_stopped()` | 写一条 `STOP` 操作 |

**内部方法**

| 方法 | 作用 |
| --- | --- |
| `_put(item)` | `put_nowait` 入队；队列满等异常只累加 `dropped`，绝不抛给业务线程 |
| `_run()` | 写线程主循环：建库→连接→写一条 `server_instance`→就绪→循环取批→flush→退出时写完剩余、补 `stopped_at`、关连接 |
| `_drain()` | 取一批：先阻塞等 0.5 秒拿第一条，再非阻塞地把队列里最多 200 条一起取走 |
| `_drain_all()` | 退出时把队列剩余全部取出 |
| `_flush(batch)` | 一个事务里：批量插日志、逐条插操作、处理房间创建/关闭、逐条存消息；失败则整批丢弃并记录 `errors/dropped` |
| `_ensure_instance(conn)` | 插入一行 `server_instance`，返回 id |
| `_close_instance()` | 补 `stopped_at` |
| `_room_id(room)` | hub 房间 → `chat_room.id`，先查缓存再查库，没有就插入 |
| `_close_room(room, reason)` | 补 `closed_at`/`close_reason`，清缓存 |
| `_user_id(nickname)` | 昵称 → `chat_user.id`，有则更新 `last_seen`，无则插入 |
| `_store_message(row)` | 插入 `chat_message`；如果内容是"X 加入了聊天室/离开了聊天室"，顺带维护 `user_session` |

为什么 `_flush` 里要把批次按类型分组：一个事务里先批量 `executemany` 日志（最快），
再处理需要查缓存/查库的操作和消息，减少往返次数。

**线程模型图**

```text
聊天线程（多个）                 写线程（1 个）
   │ LOGGER.info(...)               │
   ▼                                │
QueueLogHandler.emit                │
   │ enqueue_log                    │
   ▼                                │
_queue ────────────────────────────►│ _drain() 每 0.5s/200 条
                                    ▼
                                 _flush() 单事务 executemany
                                    ▼
                              data/chat_ops.db
```

只有写线程碰数据库写连接，所以不会出现"多线程同时写 SQLite 报 database is locked"。

### 5.6 `importer.py`：导入历史日志

| 函数 | 作用 |
| --- | --- |
| `parse_line(line, default_date)` | 解析一行日志，返回 `{ts, level, logger, message, ...}`；空行返回 `None`；格式不认识时按 INFO 原文兜底 |
| `import_log_file(conn, path, instance_id=0, chunk_size=200)` | 逐行读文件、分类、每 200 行批量插入；返回文件/导入数/跳过数/首末时间 |
| `_insert(conn, rows)` | 一个事务里 `executemany` 插入 `log_event` |
| `build_parser()` / `main(argv=None)` | 命令行入口：`python -m src.task2_db_system.importer <文件> [--db]` |

`parse_line` 支持两种格式：

- `2026-11-27 10:00:00,123 INFO 消息`（任务1 的 logging 默认格式）
- `10:00:00 INFO 消息`（只有时间，日期用 `default_date` 补）

`import_log_file` 在没有实例时自动建一个名为"历史日志导入"的 `server_instance`，
这样外键有值可指；导入成功后在 CLI 里还会写一条 `LOG_IMPORT` 操作记录。

### 5.7 `seed.py`：演示数据

| 函数 | 作用 |
| --- | --- |
| `_seed_logs(conn, instance_id)` | 生成 120 条日志（含 ERROR/WARNING/DEBUG，分布在最近几天） |
| `_seed_messages(conn, instance_id)` | 生成 60 条聊天/私聊/系统消息 |
| `_seed_operations(conn, instance_id)` | 生成 12 条操作审计 |
| `_seed_incidents(conn, instance_id)` | 生成 3 条事故（待处理/处理中/已关闭各一） |
| `seed(db_path=None, reset=False)` | 总入口；已存在且 `reset=False` 时返回 `skipped` |
| `_purge_demo(conn, instance_id)` | 删掉上一轮演示实例及其关联数据（保留全局用户） |
| `build_parser()` / `main(argv=None)` | `python -m src.task2_db_system.seed [--reset]` |

固定随机种子 `SEED = 20261127`，保证每次生成的数据一样，报告截图可复现。

### 5.8 `cli.py`：字符界面

#### 5.8.1 输出与输入辅助

| 函数 | 名字拆解 | 作用 |
| --- | --- | --- |
| `_width(text)` | width=宽度 | 计算显示宽度：中文算 2 列，英文算 1 列（`east_asian_width`） |
| `_clip(text, limit)` | clip=裁剪 | 超长截断并加 `…` |
| `_pad(text, width)` | pad=填充 | 右侧补空格对齐 |
| `format_table(headers, rows, max_col=30)` | format=格式化 | 生成带边框的 ASCII 表格行列表 |
| `print_table(headers, rows)` | | 打印上面生成的表格 |
| `ask(prompt, default="")` | ask=询问 | 读一行输入；`EOFError` 返回默认值（管道输入结束时不会崩） |
| `ask_int(prompt, default=None)` | | 读整数，非法输入返回默认值 |
| `ask_choice(prompt, options, allow_empty=True)` | choice=选项 | 读一个选项，支持回车表示"不限" |
| `ask_index(prompt, total, default=1)` | index=下标 | 读页码，限制在 1~total |
| `_page_loop(title, fetch)` | page loop=翻页循环 | 通用分页器：`n` 下一页、`p` 上一页、数字跳页、`s` 改每页条数、`q` 返回 |
| `_split_rows(title, rows)` | split=拆分 | 按视图标题取对应表头 |

`format_table` 为什么要自己算宽度：`str.ljust()` 按字符数补空格，
但中文在终端里占两列，直接用 `ljust` 会错位。

#### 5.8.2 浏览与维护

| 函数 | 作用 |
| --- | --- |
| `browse_logs(repo)` | 依次问关键字/级别/类型/房间/人员/起止/排序，再分页展示 |
| `browse_messages(repo)` | 关键字/类型/房间/发送者/时间/排序 |
| `browse_operations(repo)` | 关键字/动作/操作者/结果/时间/排序 |
| `browse_incidents(repo)` | 关键字/状态/级别/排序 |
| `incident_menu(repo)` | 事故子菜单：1 新建 2 修改 3 删除 0 返回 |
| `create_incident(repo)` | 询问标题/级别/状态/负责人/描述/关联日志 id，然后 `repo.create_incident` |
| `update_incident(repo)` | 先列出事故，选 id，再逐项询问（空回车=不改） |
| `delete_incident(repo)` | 选中后要求输入 `yes` 二次确认 |
| `browse_catalog(repo)` | 基础数据：实例/房间/用户/会话 |
| `show_statistics(conn, repo)` | 打印概览、级别分布、24 小时柱状图、事故状态、动作 Top |
| `import_menu(repo)` | 输入日志文件路径 → `import_log_file` → 写 `LOG_IMPORT` 操作 |
| `export_menu(repo)` | 输入目标路径（默认 `data/logs-export.csv`）→ 写 CSV（`utf-8-sig`） |
| `purge_menu(repo)` | 输入截止时间 + `yes` 确认 → `purge_logs` → 写 `LOG_PURGE` 操作 |
| `demo(conn, repo)` | 无人值守演示：打印四视图最新 5 条 + 统计 |
| `build_parser()` | 定义 `--db`、`--demo`、`--stats` |
| `main(argv=None)` | 入口：解析参数 → 打开库 → 分支执行；`Ctrl+C` 返回 130 |

菜单本身：

```text
 1. 运行日志    浏览 / 筛选 / 排序 / 分页
 2. 聊天记录    按房间、发送者、类型查询
 3. 操作审计    管理员动作记录
 4. 事故记录    增 / 删 / 改 / 查
 5. 统计概览
 6. 基础数据
 7. 导入日志
 8. 导出 CSV
 9. 清理日志
 0. 退出
```

### 5.9 `audit_panel.py`：监控台里的查询界面

#### 5.9.1 视图配置

`VIEWS = ("运行日志", "操作审计", "聊天记录", "事故记录")`

`VIEW_CONFIG` 是"界面配置表"，每个视图包含三样东西：

- `columns`：`(字段名, 表头文字, 列宽)`；
- `filters`：筛选下拉的 `(显示文字, 实际值)`；
- `sortable`：允许点击排序的字段（必须和 repository 的排序白名单对得上）。

这样加一个视图只要加一段配置，不用改一堆 if。

#### 5.9.2 `class AuditPanel(ttk.Frame)`

| 方法 | 作用 |
| --- | --- |
| `_short(value, limit=90)` | 把多行文本压成一行并截断，表格列里只显示前 90 字，完整内容双击查看 |
| `__init__(master, db_path=None)` | 建变量、建界面、首次刷新 |
| `_build_ui()` | 顶部筛选行、概览标签、分页行、表格、提示行；绑定下拉事件、回车查询、表头排序、双击详情 |
| `_apply_view_config()` | 切视图时重建列、重建筛选项、启用/禁用事故按钮，并记下 `_view_applied` |
| `_ensure_conn()` | 懒连接：第一次用时才 `connect` + 建 `Repository`；库不存在时提示"启动注册中心后自动建库" |
| `_selected_filter()` | 把下拉框显示文字翻译成实际值 |
| `refresh()` | 核心：校验排序键 → 按当前视图调用 repository → 填表格 → 更新分页与概览 |
| `_refresh_summary()` | 调 `stats.overview` 更新顶部"日志 120 条（错误 13…）" |
| `switch_view()` | 下拉框事件处理：重置页码、排序键后 refresh |
| `search()` | "查询"按钮：页码归 1 再 refresh |
| `reset()` | 清空关键字/时间/筛选，恢复默认 |
| `sort_by(key)` | 点表头：同一列则切换升降序，否则换列并降序 |
| `prev_page()` / `next_page()` | 翻页（边界处不动） |
| `_change_page_size()` | 改每页条数后回到第 1 页 |
| `show_detail()` | 双击行弹 `Toplevel`，显示该行所有字段的完整内容 |
| `_selected_incident()` | 取选中行并回库查完整记录（列表里是截断文本） |
| `add_incident()` | 打开 `IncidentDialog`，保存后 refresh |
| `edit_incident()` | 带当前值打开对话框 |
| `delete_incident()` | `askyesno` 二次确认后删除 |
| `export_csv()` | 用 `filedialog.asksaveasfilename` 选路径，把**当前页**的记录（含所有列）写成 CSV（utf-8-sig） |
| `close()` | 关闭数据库连接（窗口关闭时调用） |

`refresh()` 里两个"补丁"值得注意，它们修的是真实 bug：

1. 程序化切换视图不会触发下拉框事件，`sort_key` 可能还是上一个视图的 `ts`，
   而事故视图没有 `ts` 列，会弹"不支持按 ts 排序"。所以先校验、回退默认排序。
2. 同理，列和筛选项可能还是上一个视图的，所以比较 `_view_applied` 后重建。

#### 5.9.3 `class IncidentDialog(tk.Toplevel)`

| 方法 | 作用 |
| --- | --- |
| `__init__(master, row=None)` | 建表单：标题、级别、状态、负责人、关联日志 id、描述；`row` 有值时预填 |
| `_on_save()` | 校验标题非空，把输入收集成 dict，`destroy()` 关闭 |
| `show()` | `wait_window` 阻塞等待，返回 dict 或 `None`（取消） |

#### 5.9.4 `build_audit_tab(master, db_path=None)`

工厂函数，返回一个 `AuditPanel`，方便在别处复用这个标签页。

### 5.10 `gui_server.py`：任务1 监控台里的接线

| 位置/方法 | 作用 |
| --- | --- |
| `MonitorApp.__init__(..., db_path=None)` | 记录 `self.audit_db_path`（默认 `DEFAULT_DB_PATH`），`self.recorder=None` |
| `_build_ui()` 末尾 | 在右侧 Notebook 加第 3 页"数据库审计（任务2）" |
| `_start_audit(host, port)` | 创建 `AuditRecorder` → `start()` → `attach_logging("task1", INFO)` → 挂 hub 三个回调 → 写 `START` → 刷新面板；失败只提示"本次运行不记录" |
| `_stop_audit()` | 摘回调、`recorder.stop()`、刷新面板 |
| `_audit(action, actor="管理员", target="", room_name="", result="ok", detail="")` | 便捷方法：转发给 `recorder.record_operation`，没有记录器时静默跳过 |
| `start_hub()` | 启动成功后调 `_start_audit(bound_host, bound_port)` |
| `stop_hub()` | 顺序：摘 `on_room_event` → 写 `STOP` → `hub.shutdown()` → 清回调 → `_stop_audit()` |
| `create_room()` / `close_selected_room()` | 成功/失败都写 `ROOM_CREATE` / `ROOM_CLOSE` |
| `kick_selected_user()` | 先查房间名，再写 `KICK`（失败写 `result="failed"`） |
| `broadcast_all()` | 写 `BROADCAST`，detail 截前 100 字 |
| `approve_selected_request()` / `reject_selected_request()` | 写 `ROOM_APPROVE` / `ROOM_REJECT` |
| `build_parser()` | 新增 `--db`（默认 `data/chat_ops.db`） |
| `main()` | 把 `args.db` 传给 `MonitorApp` |

停止顺序很重要：先写 `STOP` 再 `hub.shutdown()`，否则关闭房间产生的日志会在记录器停止后丢失。

### 5.11 `hub.py`：三个新回调

`ChatHub.__init__` 新增：

- `on_room_created(room)`：房间创建后回调；
- `on_room_closed(room, reason)`：房间关闭后回调；
- `on_room_activity(room, message)`：房间内每来一条消息回调。

新增 `_notify_room(callback, room, *args, **kwargs)`：

- 名字：notify=通知。
- 回调不存在（`None`）就什么也不做；
- 回调抛异常只 `LOGGER.warning("房间审计回调失败: %s", exc)`。
  这是 **fail-open**：审计功能坏了，聊天必须照常。

调用点：

- `create_room()` 里 `LOGGER.info("[聊天室] 创建...")` 之后；
- `close_room()` 里 `LOGGER.info(...)` 之后；
- `_record_room_event()` 里先通知 `on_room_activity`，再调原来的 `on_room_event`。

### 5.12 测试文件

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_task2_db.py` | 建表/WAL/CHECK/唯一约束；LIKE 转义、时间范围、排序白名单、分页；消息/操作/事故增删改、会话关闭、清理日志；行数统计 |
| `tests/test_task2_recorder.py` | 实例创建与关闭、日志分类、房间消息→会话、关闭原因、库不可用时降级、文本分类 |
| `tests/test_task2_cli.py` | 日志解析两种格式、空行跳过、缺文件报错、空文件；`--demo`/`--stats`/空输入退出 |

测试数据库不建在系统临时目录，而是 `data/` 下 `test-*.db` 临时文件，测完连同 `-wal/-shm` 一起删
（原因见 9.6）。

---

## 6. 跟着一条数据走一遍

### 例 1：小明进入房间

```text
客户端连接房间端口
  → server.py 收到 join，LOGGER.info("... 以昵称 小明 加入")
  → QueueLogHandler.emit → recorder.enqueue_log
  → log_event 多一行：level=INFO, event_type=加入, actor=小明, message=...

hub 把系统消息 "小明 加入了聊天室" 推给房间
  → hub._record_room_event → on_room_activity(room, message)
  → recorder.record_room_message → chat_message 多一行：kind=system
  → _store_message 发现内容是 "X 加入了聊天室"
     → ensure_user("小明") → chat_user 多一行
     → INSERT user_session（joined_at=现在，left_at=NULL）
```

### 例 2：管理员踢人

```text
监控台点"踢出选中"
  → gui_server.kick_selected_user()
  → hub.kick(room_id, name) 成功
  → self._audit("KICK", target=name, room_name=房间名)
  → recorder.record_operation("KICK", actor="管理员", ...)
  → operation 多一行：action=KICK, actor=管理员, result=ok
  （hub 内部还会 LOGGER.info，于是 log_event 也有一条"[踢出] ..."）
```

### 例 3：打开"数据库审计"标签页看到数据

```text
AuditPanel.refresh()
  → repo.list_logs(level=..., page=1, page_size=50)
  → SELECT ... FROM log_event WHERE ... ORDER BY ts DESC LIMIT 50 OFFSET 0
  → 行填进 Treeview；stats.overview 更新顶部概览
```

---

## 7. 十个关键设计决策

| 决策 | 为什么 |
| --- | --- |
| 日志用 logging Handler 接入 | 不用改任务1 的任何日志语句，新增功能零侵入 |
| 房间/消息用 hub 回调 | 文本日志只能"猜"，回调能拿到房间对象、消息类型等准确字段 |
| 独立写线程 + 队列 | 网络线程不能被磁盘 I/O 和 SQLite 锁拖慢 |
| 每 0.5 秒/200 条批量事务 | 逐条 commit 慢一个数量级，还会频繁锁库 |
| 写库失败 fail-open | 审计是附加功能，绝不能因为它让聊天服务崩溃 |
| WAL 模式 | 界面读日志和服务器写日志可以同时进行 |
| 排序走白名单 | `ORDER BY` 不能用占位符，只能靠白名单防注入 |
| 时间统一存 TEXT | 格式固定时字符串比较=时间比较，还能 `substr` 按天/小时分组 |
| 日志与聊天分表 | 查询互不拖累，清理日志不会误删聊天 |
| `ensure_*` 幂等写入 | 同一房间/用户会反复出现，ensure 避免重复行和外键错误 |

---

## 8. 自己动手运行

### 8.1 环境

Python 3.7+ 即可，**不需要安装任何第三方库**（全部标准库）。
如果你的 `python` 不在 PATH，把下面命令里的 `python` 换成你的解释器路径。

### 8.2 生成演示数据

```bash
python -m src.task2_db_system.seed --reset
```

输出应类似：

```text
演示数据已写入 ...\data\chat_ops.db
  server_instance 1
  chat_room       3
  chat_user       8
  ...
```

### 8.3 字符界面

```bash
python -m src.task2_db_system.cli          # 交互菜单
python -m src.task2_db_system.cli --demo   # 自动打印四视图+统计
python -m src.task2_db_system.cli --stats  # 只打印统计
```

### 8.4 监控台（含数据库标签页）

```bash
python src/task1_socket_chat/gui_server.py --host 127.0.0.1 --port 0
```

启动后：点"启动注册中心" → 建房间 → 找同学/自己开客户端进房聊几句 → 关房间 → 停服，
再切到"数据库审计（任务2）"，四个视图里应该都有新数据。

### 8.5 导入历史日志

```bash
python -m src.task2_db_system.importer logs/server-20261127.log
```

### 8.6 想直接看数据库内容

- 用本项目的 CLI（推荐，已中文化）；
- 或用免费工具 DB Browser for SQLite 打开 `data/chat_ops.db`；
- 注意：打开前最好先停掉监控台，避免同时写。

### 8.7 跑测试

```bash
python -m unittest tests.test_task2_db tests.test_task2_recorder tests.test_task2_cli -v
python -m unittest discover -s tests
```

---

## 9. 常见问题 FAQ

**9.1 数据库文件在哪？删了会怎样？**
`data/chat_ops.db`（还有 `-wal`、`-shm` 两个伴随文件）。删掉后下次启动会自动重建空库；
想恢复演示数据跑 `python -m src.task2_db_system.seed --reset`。

**9.2 为什么 `data/` 不在 git 里？**
`.gitignore` 忽略了它：数据库是运行数据，会越变越大，也不该提交。

**9.3 为什么日志表里有些 `instance_id` 是空的？**
历史日志导入、日志清理这类操作不属于某一次服务器运行，所以允许 NULL。

**9.4 CSV 用 Excel 打开中文乱码怎么办？**
本项目导出用的是 `utf-8-sig`（带 BOM），Excel 能正确识别。若用其它工具导出，
记得也选 UTF-8 with BOM 或 GBK。

**9.5 `database is locked` 是什么？**
SQLite 同一时刻只允许一个写者。本项目用 WAL + 单写线程 + 5 秒等待避免了它；
如果你自己另开程序写同一个库，可能短暂出现，重试即可。

**9.6 为什么测试的临时数据库放在 `data/` 而不是系统临时目录？**
当前开发机的沙箱只允许在仓库根、`data/`、`%TEMP%` 根目录写文件，
在 `%TEMP%` 下新建子目录建库会报 `unable to open database file`。
换成普通电脑后可以改回 `tempfile`，代码逻辑不变。

**9.7 想改成 MySQL，最快怎么验证？**
先只改 `db.py` 的 `connect()`，再全局把 `?` 换成 `%s`，跑
`python -m src.task2_db_system.cli --demo`。报错会集中在 SQL 方言差异上。

**9.8 为什么函数名有下划线开头？**
`_like`、`_paged`、`_flush` 这种单下划线表示"模块/类内部使用"，
Python 不会强制限制，但这是告诉读者"外部不要直接调用"。双下划线（如 `__init__`）是 Python 特殊方法。

**9.9 界面里为什么日志和聊天记录不能改？**
审计数据的价值在于"不可篡改"。可增删改的只有事故记录和用户备注，
而且删除都会二次确认。

**9.10 `ensure_instance` 和 `insert_operation` 有什么区别？**
前者给"写线程启动时登记运行实例"用，必须返回 id 给后续外键；
后者给"人做的操作"留痕，属于业务审计。

---

## 10. 术语表与命名规律

| 词 | 含义 |
| --- | --- |
| DB / database | 数据库 |
| table / row / column | 表 / 行 / 列 |
| primary key / foreign key | 主键 / 外键 |
| index | 索引，加速查询 |
| schema | 表结构 |
| query / filter / sort / paginate | 查询 / 筛选 / 排序 / 分页 |
| transaction / commit / rollback | 事务 / 提交 / 回滚 |
| constraint / CHECK / UNIQUE | 约束 / 取值检查 / 唯一 |
| WAL | Write-Ahead Logging，SQLite 的一种日志模式 |
| recording / audit | 记录 / 审计 |
| instance | 实例，这里指一次服务器运行 |
| session | 会话，这里指一次进入房间 |
| incident | 事故 |
| severity / status / assignee | 严重级别 / 状态 / 负责人 |
| queue / batch / flush | 队列 / 批次 / 落库 |
| handler | 处理器，logging 的输出端 |
| ensure | 确保存在（有则复用，无则创建） |
| normalize | 规范化（统一格式/校验取值） |
| classify | 分类 |
| parse | 解析 |
| export / import / purge | 导出 / 导入 / 清理 |
| scalar / overview / summary | 单个数值 / 概览 / 汇总 |

命名规律：

- `list_xxx`：查列表，通常带筛选和分页；
- `get_xxx`：按主键取一条；
- `create_xxx` / `insert_xxx`：新增；
- `update_xxx` / `close_xxx` / `stop_xxx`：修改；
- `delete_xxx` / `purge_xxx`：删除；
- `ensure_xxx`：幂等写入；
- `_xxx`：内部函数，外部不要调用；
- `xxx_counts` / `xxx_summary`：统计。

---

## 附：源码清单

| 文件 | 行数（约） | 内容 |
| --- | --- | --- |
| `src/task2_db_system/config.py` | 96 | 常量与枚举校验 |
| `src/task2_db_system/schema.sql` | 145 | 表结构与索引 |
| `src/task2_db_system/db.py` | 126 | 连接、建库、事务 |
| `src/task2_db_system/repository.py` | 673 | 查询与增删改 |
| `src/task2_db_system/stats.py` | 145 | 统计 |
| `src/task2_db_system/recorder.py` | 554 | 日志/消息记录器 |
| `src/task2_db_system/importer.py` | 152 | 历史日志导入 |
| `src/task2_db_system/seed.py` | 311 | 演示数据 |
| `src/task2_db_system/cli.py` | 572 | 字符界面 |
| `src/task1_socket_chat/audit_panel.py` | 567 | 监控台审计面板 |
| `src/task1_socket_chat/gui_server.py` | 793 | 监控台（含任务2 接线） |
| `src/task1_socket_chat/hub.py` | 628 | 房间管理（含回调） |

> 文档依据当前仓库代码整理；若代码更新，以代码为准。
