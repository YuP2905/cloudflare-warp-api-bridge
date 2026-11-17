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

WARP_INTERFACE_NAME = settings.cloudflare_warp_interface_name  # WARP 网络接口名称
SOCKET_OPTIONS = settings.socket_options                       # TCP Socket 选项, 用于绑定 WARP 网络接口
KEEPALIVE_OPTIONS = settings.keepalive_options                 # TCP Socket Keepalive 选项, 用于定期探测连接状态

class WarpInterfaceUnavailableError(OSError):
    """绑定 Socket 时找不到指定的 WARP 网络接口"""
    pass

def bind_socket_to_warp_network_interface(
    upstream_socket: "Socket",
):
    try:
        upstream_socket.setsockopt(
            SOCKET_OPTIONS["level"],
            SOCKET_OPTIONS["optname"],
            SOCKET_OPTIONS["value"],
        )
    except OSError as e:
        if e.errno == errno.ENODEV:   # 19, 设备不存在时
            # 捕获设备不存在的异常
            raise WarpInterfaceUnavailableError(
                f"WARP interface '{WARP_INTERFACE_NAME}' not found.",
            ) from e
        # 其他 OSError 异常继续向上抛出
        raise

def enable_tcp_socket_keepalive(
    upstream_socket: "Socket",
):
    # 开启 keepalive, 定期探测
    for K_OP in KEEPALIVE_OPTIONS:
        upstream_socket.setsockopt(
            K_OP["level"],
            K_OP["optname"],
            K_OP["value"],
        )
