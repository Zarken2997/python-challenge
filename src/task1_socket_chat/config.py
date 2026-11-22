"""集中管理默认值与配置校验，避免端口、超时等魔法数字散落在各模块。"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9000
DEFAULT_UDP_PORT = 9100

MAX_ROOMS = 20
"""同时存在的聊天室上限（每个聊天室占用一个端口）。"""

MAX_NAME_LENGTH = 16
"""昵称最大长度（字符数）。"""

MAX_TEXT_LENGTH = 4000
"""单条聊天正文最大长度（字符数），远小于协议帧上限，留出昵称与字段开销。"""


def sanitize_name(name: str) -> str:
    """清理昵称：去掉首尾空白与控制字符，截断超长部分；空名回退为 ``guest``。"""
    cleaned = "".join(ch for ch in name if ch.isprintable()).strip()
    cleaned = cleaned[:MAX_NAME_LENGTH]
    return cleaned or "guest"


@dataclass(frozen=True)
class ServerConfig:
    """服务器参数。端口 0 表示由系统分配（测试常用）。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    backlog: int = 16
    max_clients: int = 50
    ping_interval: float = 30.0
    max_missed_pings: int = 2
    handshake_timeout: float = 10.0

    def __post_init__(self) -> None:
        if not 0 <= self.port <= 65535:
            raise ValueError(f"端口必须在 0-65535 之间: {self.port}")
        if self.backlog < 1:
            raise ValueError(f"backlog 必须 >= 1: {self.backlog}")
        if self.max_clients < 1:
            raise ValueError(f"max_clients 必须 >= 1: {self.max_clients}")
        if self.ping_interval <= 0:
            raise ValueError(f"ping_interval 必须 > 0: {self.ping_interval}")
        if self.max_missed_pings < 1:
            raise ValueError(f"max_missed_pings 必须 >= 1: {self.max_missed_pings}")


@dataclass(frozen=True)
class HubConfig:
    """注册中心（控制端口）参数：房间列表、开房申请都走这个端口。

    聊天室本身各自监听一个端口，端口号由系统分配；控制端口固定（默认 9000），
    这样客户端启动时才知道该连哪里。
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    backlog: int = 16
    max_clients: int = 50
    ping_interval: float = 30.0
    max_missed_pings: int = 2
    handshake_timeout: float = 10.0
    max_rooms: int = MAX_ROOMS

    def __post_init__(self) -> None:
        if not 0 <= self.port <= 65535:
            raise ValueError(f"端口必须在 0-65535 之间: {self.port}")
        if self.backlog < 1:
            raise ValueError(f"backlog 必须 >= 1: {self.backlog}")
        if self.max_clients < 1:
            raise ValueError(f"max_clients 必须 >= 1: {self.max_clients}")
        if self.ping_interval <= 0:
            raise ValueError(f"ping_interval 必须 > 0: {self.ping_interval}")
        if self.max_missed_pings < 1:
            raise ValueError(f"max_missed_pings 必须 >= 1: {self.max_missed_pings}")
        if self.max_rooms < 1:
            raise ValueError(f"max_rooms 必须 >= 1: {self.max_rooms}")


@dataclass(frozen=True)
class UdpRelayConfig:
    """UDP 中继参数；UDP 无连接，用 TTL 清理长时间不发言的节点。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_UDP_PORT
    peer_ttl: float = 120.0
    max_peers: int = 50

    def __post_init__(self) -> None:
        if not 0 <= self.port <= 65535:
            raise ValueError(f"端口必须在 0-65535 之间: {self.port}")
        if self.peer_ttl <= 0:
            raise ValueError(f"peer_ttl 必须 > 0: {self.peer_ttl}")
        if self.max_peers < 1:
            raise ValueError(f"max_peers 必须 >= 1: {self.max_peers}")


@dataclass(frozen=True)
class ClientConfig:
    """客户端参数。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    name: str = "guest"
    connect_timeout: float = 5.0
    welcome_timeout: float = 10.0
    reconnect_attempts: int = 3
    reconnect_delay: float = 1.0
    linger: float = 1.0

    def __post_init__(self) -> None:
        if not 0 <= self.port <= 65535:
            raise ValueError(f"端口必须在 0-65535 之间: {self.port}")
        if self.connect_timeout <= 0:
            raise ValueError(f"connect_timeout 必须 > 0: {self.connect_timeout}")
        if self.reconnect_attempts < 1:
            raise ValueError(f"reconnect_attempts 必须 >= 1: {self.reconnect_attempts}")
        if self.linger < 0:
            raise ValueError(f"linger 必须 >= 0: {self.linger}")
