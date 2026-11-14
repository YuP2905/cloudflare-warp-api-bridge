from __future__ import annotations
from typing import (
    TYPE_CHECKING,
)
if TYPE_CHECKING:
    from socket import (
        socket as Socket,
    )

# settings
from config import settings

SOCKET_OPTIONS = settings.socket_options

def bind_warp_interface(
    upstream_socket: "Socket",
) -> None:
    # 每次创建上游连接时, 都为新的 Socket 绑定 WARP 接口
    # 此操作只指定出口接口, 不创建隧道, 也不修改系统路由
    # 接口不存在或绑定失败时, 异常交给调用方处理, 不改用其他接口
    upstream_socket.setsockopt(
        SOCKET_OPTIONS["level"],
        SOCKET_OPTIONS["optname"],
        SOCKET_OPTIONS["value"],
    )
