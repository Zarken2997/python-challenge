"""任务2：聊天服务器运维日志库（SQLite 微型管理信息系统）。

对外主要入口：

- :func:`task2_db_system.db.init_db`：建库/升级表结构；
- :class:`task2_db_system.repository.Repository`：参数化的浏览、排序、增删改查；
- :class:`task2_db_system.recorder.OperationRecorder`：把任务1 服务器的日志与操作写入数据库；
- ``python -m src.task2_db_system.cli`` / ``.gui``：字符界面与图形界面。
"""

from __future__ import annotations

from .config import APP_NAME, APP_VERSION, DEFAULT_DB_PATH

__all__ = ["APP_NAME", "APP_VERSION", "DEFAULT_DB_PATH", "__version__"]

__version__ = APP_VERSION
