"""任务1：基于 Python socket 的 TCP/UDP 实时通信框架。

模块划分（对应题目“socket 工作流程”知识点）：

- ``protocol``：TCP 分帧协议（长度前缀 + JSON），解决粘包/半包；
- ``server``：TCP 多客户端服务器（每连接一个线程，一点对多点）；
- ``client``：交互式客户端（接收线程 + 输入线程）；
- ``udp_chat``：UDP 中继演示，用于对比 TCP 的可靠性差异；
- ``config``：集中默认值与校验。
"""

from .protocol import (
    PROTOCOL_VERSION,
    ConnectionClosed,
    Message,
    ProtocolError,
    read_message,
    send_message,
)

__all__ = [
    "PROTOCOL_VERSION",
    "ConnectionClosed",
    "Message",
    "ProtocolError",
    "read_message",
    "send_message",
]
__version__ = "1.0.0"
