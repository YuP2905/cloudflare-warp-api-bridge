from __future__ import annotations
from typing import (
    TYPE_CHECKING,
)
if TYPE_CHECKING:
    from socket import (
        socket as Socket,
    )

import errno

# settings
from config import settings

SOCKET_OPTIONS = settings.socket_options
KEEPALIVE_OPTIONS = settings.keepalive_options
WARP_INTERFACE_NAME = settings.cloudflare_warp_interface_name

class WarpInterfaceUnavailableError(OSError):
    """WARP 网络接口不存在, 通常是隧道尚未建立或正在重连."""

def bind_warp_interface(
    upstream_socket: "Socket",
) -> None:
    # 每次创建上游连接时, 都为新的 Socket 绑定 WARP 接口
    # 此操作只指定出口接口, 不创建隧道, 也不修改系统路由
    # 接口不存在或绑定失败时, 异常交给调用方处理, 不改用其他接口
    try:
        upstream_socket.setsockopt(
            SOCKET_OPTIONS["level"],
            SOCKET_OPTIONS["optname"],
            SOCKET_OPTIONS["value"],
        )
    except OSError as e:
        # 接口不存在 (隧道尚未建立或正在重连) 属于预期情况, 单独区分
        if e.errno == errno.ENODEV:
            raise WarpInterfaceUnavailableError(
                errno.ENODEV,
                f"WARP interface '{WARP_INTERFACE_NAME}' does not exist",
            ) from e
        raise

def enable_keepalive(
    upstream_socket: "Socket",
) -> None:
    # 隧道中途断开时, 上游连接可能既收不到数据也收不到断开通知
    # 开启 keepalive 后, 内核会定期探测, 失效时让读写报错, 从而关闭这条桥接连接
    for option in KEEPALIVE_OPTIONS:
        upstream_socket.setsockopt(
            option["level"],
            option["optname"],
            option["value"],
        )
