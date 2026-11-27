"""任务2 运维日志库的集中配置：路径、枚举、分页与内容上限。

把"魔法值"集中在这里，是为了三件事：
1. 界面下拉框、命令行校验、数据库 CHECK 约束共用同一份枚举，避免三处各写一遍；
2. 报告里的表结构说明可以直接引用这里的常量；
3. 换数据库路径或分页大小时只改一处。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = PACKAGE_DIR / "schema.sql"

DEFAULT_DB_PATH = REPO_ROOT / "data" / "chat_ops.db"
"""数据库文件默认位置。``data/`` 已被 .gitignore 忽略，不会把运行数据提交进仓库。"""

APP_NAME = "聊天服务器运维日志库"
APP_VERSION = "1.0.0"

PAGE_SIZE = 50
"""列表默认每页条数；分页是任务2「浏览」的强制要求之一。"""

PAGE_SIZES = (20, 50, 100, 200)
MAX_EXPORT_ROWS = 20000
"""单次导出的上限，防止误点「导出」把几十万行日志写成一个巨大 CSV。"""

CONTENT_LIMIT = 4000
"""``chat_message.content`` 的截断长度（字符）。超长消息保留前 4000 字并注明截断。"""

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

EVENT_TYPES = (
    "启动",
    "停止",
    "加入",
    "离开",
    "聊天",
    "私聊",
    "系统",
    "创建房间",
    "关闭房间",
    "申请",
    "批准",
    "拒绝",
    "踢出",
    "公告",
    "错误",
    "其他",
)
"""运行日志的归类。由 :func:`task2_db_system.recorder.classify_event` 按关键字推断。"""

MESSAGE_KINDS = ("chat", "private", "system")
MESSAGE_KIND_LABELS = {"chat": "群聊", "private": "私聊", "system": "系统"}

ACTION_LABELS = {
    "START": "启动服务",
    "STOP": "停止服务",
    "ROOM_CREATE": "创建房间",
    "ROOM_CLOSE": "关闭房间",
    "BROADCAST": "全服公告",
    "KICK": "踢出用户",
    "ROOM_REQUEST": "开房申请",
    "ROOM_APPROVE": "批准申请",
    "ROOM_REJECT": "拒绝申请",
    "LOG_IMPORT": "导入日志",
    "LOG_PURGE": "清理日志",
    "INCIDENT_CREATE": "新建事故",
    "INCIDENT_UPDATE": "修改事故",
    "INCIDENT_DELETE": "删除事故",
    "OTHER": "其他",
}
ACTIONS = tuple(ACTION_LABELS)

RESULT_LABELS = {"ok": "成功", "failed": "失败"}

SEVERITIES = ("低", "中", "高")
STATUSES = ("待处理", "处理中", "已关闭")

PROTOCOLS = ("tcp", "udp")


def normalize_choice(value: str, allowed: tuple[str, ...], field: str) -> str:
    """校验枚举值：空串表示"不筛选"，其余必须精确命中允许值。

    统一抛 ``ValueError``，调用方（GUI/CLI）负责转成用户能看懂的提示；
    数据库层的 CHECK 约束是最后一道防线，不能替代这里的校验。
    """
    text = (value or "").strip()
    if text == "":
        return ""
    if text not in allowed:
        raise ValueError(f"{field} 只能是 {'/'.join(allowed)}，收到: {value!r}")
    return text
