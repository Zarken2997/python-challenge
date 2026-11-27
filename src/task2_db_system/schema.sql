-- 任务2：聊天服务器运维日志库（SQLite）
--
-- 设计原则：
-- 1. 机器产生的数据（运行日志、聊天记录、会话）只追加，便于审计；
-- 2. 人需要维护的数据（事故处置记录、备注）才提供增删改；
-- 3. 所有时间统一存 TEXT 'YYYY-MM-DD HH:MM:SS'，可直接比较、排序、按天分组；
-- 4. 外键全部声明 ON DELETE 行为，并在连接时 PRAGMA foreign_keys=ON 强制生效。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '1');

-- 一次服务器运行。关闭注册中心时补 stopped_at，而不是新开一行。
CREATE TABLE IF NOT EXISTS server_instance (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL DEFAULT '',
    host       TEXT NOT NULL DEFAULT '',
    port       INTEGER NOT NULL DEFAULT 0 CHECK (port BETWEEN 0 AND 65535),
    protocol   TEXT NOT NULL DEFAULT 'tcp' CHECK (protocol IN ('tcp', 'udp')),
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    note       TEXT NOT NULL DEFAULT '',
    CHECK (stopped_at IS NULL OR stopped_at >= started_at)
);

-- 聊天室。hub_room_id 是任务1 ChatHub 内存里的房间号，同一个实例内唯一。
CREATE TABLE IF NOT EXISTS chat_room (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES server_instance(id) ON DELETE CASCADE,
    hub_room_id INTEGER NOT NULL DEFAULT 0,
    name        TEXT NOT NULL,
    host        TEXT NOT NULL DEFAULT '',
    port        INTEGER NOT NULL DEFAULT 0 CHECK (port BETWEEN 0 AND 65535),
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    closed_at   TEXT,
    close_reason TEXT NOT NULL DEFAULT '',
    UNIQUE (instance_id, hub_room_id),
    CHECK (closed_at IS NULL OR closed_at >= created_at)
);
CREATE INDEX IF NOT EXISTS idx_room_instance ON chat_room(instance_id);

-- 出现过的昵称；note 是人工可改的备注，满足 MIS 的「改」。
CREATE TABLE IF NOT EXISTS chat_user (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname   TEXT NOT NULL UNIQUE,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);

-- 一次进入/离开房间的记录。join 时插入，leave 时补 left_at。
CREATE TABLE IF NOT EXISTS user_session (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id  INTEGER NOT NULL REFERENCES server_instance(id) ON DELETE CASCADE,
    room_id      INTEGER NOT NULL REFERENCES chat_room(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES chat_user(id) ON DELETE CASCADE,
    addr         TEXT NOT NULL DEFAULT '',
    joined_at    TEXT NOT NULL,
    left_at      TEXT,
    leave_reason TEXT NOT NULL DEFAULT '',
    CHECK (left_at IS NULL OR left_at >= joined_at)
);
CREATE INDEX IF NOT EXISTS idx_session_room ON user_session(room_id);
CREATE INDEX IF NOT EXISTS idx_session_user ON user_session(user_id);

-- 服务器运行日志（logging 模块的每条记录一行）。
CREATE TABLE IF NOT EXISTS log_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 允许 NULL：历史日志导入、服务启动前的操作没有对应的运行实例。
    instance_id INTEGER REFERENCES server_instance(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    logger      TEXT NOT NULL DEFAULT '',
    event_type  TEXT NOT NULL DEFAULT '其他',
    room_name   TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    thread      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON log_event(ts);
CREATE INDEX IF NOT EXISTS idx_log_level ON log_event(level);
CREATE INDEX IF NOT EXISTS idx_log_type ON log_event(event_type);
CREATE INDEX IF NOT EXISTS idx_log_instance ON log_event(instance_id);

-- 管理操作的审计流水：谁、什么时候、对什么、做了什么、结果如何。
CREATE TABLE IF NOT EXISTS operation (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 同 log_event：日志导入、清理等全局操作可能不属于某次运行实例。
    instance_id INTEGER REFERENCES server_instance(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN (
        'START', 'STOP', 'ROOM_CREATE', 'ROOM_CLOSE', 'BROADCAST', 'KICK',
        'ROOM_REQUEST', 'ROOM_APPROVE', 'ROOM_REJECT', 'LOG_IMPORT', 'LOG_PURGE',
        'INCIDENT_CREATE', 'INCIDENT_UPDATE', 'INCIDENT_DELETE', 'OTHER')),
    actor       TEXT NOT NULL DEFAULT '',
    target      TEXT NOT NULL DEFAULT '',
    room_name   TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL DEFAULT 'ok' CHECK (result IN ('ok', 'failed')),
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_op_ts ON operation(ts);
CREATE INDEX IF NOT EXISTS idx_op_action ON operation(action);

-- 聊天正文。用户明确要求正文入库，因此单列一张表，和"运行日志"分开：
-- 查日志时不会被大段正文拖慢，删日志时也不会误删聊天记录。
CREATE TABLE IF NOT EXISTS chat_message (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES server_instance(id) ON DELETE CASCADE,
    room_id     INTEGER REFERENCES chat_room(id) ON DELETE SET NULL,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('chat', 'private', 'system')),
    sender      TEXT NOT NULL DEFAULT '',
    recipient   TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    content_len INTEGER NOT NULL DEFAULT 0 CHECK (content_len >= 0)
);
CREATE INDEX IF NOT EXISTS idx_msg_ts ON chat_message(ts);
CREATE INDEX IF NOT EXISTS idx_msg_room ON chat_message(room_id);
CREATE INDEX IF NOT EXISTS idx_msg_sender ON chat_message(sender);

-- 事故/处置记录：MIS 的「增删改」主要落在这张表，人工维护，可关联具体日志。
CREATE TABLE IF NOT EXISTS incident (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id   INTEGER REFERENCES server_instance(id) ON DELETE SET NULL,
    title         TEXT NOT NULL,
    severity      TEXT NOT NULL DEFAULT '中' CHECK (severity IN ('低', '中', '高')),
    status        TEXT NOT NULL DEFAULT '待处理' CHECK (status IN ('待处理', '处理中', '已关闭')),
    assignee      TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    log_event_id  INTEGER REFERENCES log_event(id) ON DELETE SET NULL,
    operation_id  INTEGER REFERENCES operation(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    resolved_at   TEXT,
    CHECK (resolved_at IS NULL OR resolved_at >= created_at)
);
CREATE INDEX IF NOT EXISTS idx_incident_status ON incident(status);
CREATE INDEX IF NOT EXISTS idx_incident_severity ON incident(severity);
