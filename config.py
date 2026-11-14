from __future__ import annotations
from typing import (
    List,
    Tuple,
    Optional,
    Self,
    ClassVar,
    TypedDict,
    TYPE_CHECKING,
)
if TYPE_CHECKING:
    class SocketOptions(TypedDict):
        level: int
        optname: int
        value: bytes

from pydantic import (
    IPvAnyAddress,
    Field,
)
from pydantic import (
    PrivateAttr,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)

import sys
import socket
import struct

class Settings(BaseSettings):
    # =================== 模式配置 ===================
    dev_mode: bool = Field(
        default=True,
        description="Development mode."
    )

    # =================== 本地 TCP 桥接服务配置 ===================
    # 可选(即具备默认值)的配置项
    # 本地 TCP 桥接服务的监听地址
    tcp_host: str = Field(
        default="127.0.0.1",
        description="TCP host."
    )
    # 本地 TCP 桥接服务的监听端口
    tcp_port: int = Field(
        default=8843,
        ge = 1,
        le = 65535,
        description="TCP port."
    )

    # FastAPI 服务的监听端口
    service_port: int = Field(
        default=8765,
        # TCP 端口范围: 1-65535
        ge = 1,
        le = 65535,
        description="Service port."
    )

    # 客户端 IP 地址黑名单
    block_ips: Optional[List[IPvAnyAddress]] = Field(
        default=None,
        description="List of blocked IP addresses."
    )
    # 允许的代理 IP 地址列表, 用于信任代理头部信息
    # 从而获取真实客户端 IP 地址
    trusted_proxy_ips: Optional[List[str]] = Field(
        default=None,
        description="List of trusted proxy IP addresses."
    )

    # TCP 单次读取最大字节数
    chunk_size: int = Field(
        default=65536,
        # 单次读取的最大字节数不能小于 1024
        ge=1024,
        description="Maximum TCP read size in bytes."
    )
    # 连接上游 TCP 超时时间 (秒)
    connect_timeout: float = Field(
        default=4.0,
        # TCP 连接超时时间不能小于等于 0
        gt=0,
        description="Upstream connection timeout in seconds."
    )

    # =================== Cloudflare WARP 控制 API 配置 ====================
    # Cloudflare WARP 控制 API 官方地址
    # https://developers.cloudflare.com/cloudflare-one/team-and-resources/devices/cloudflare-one-client/deployment/firewall/
    # ClassVar 不参与环境变量解析, 官方相关内容变更后需要修改源码
    # Cloudflare WARP 控制 API 官方地址
    cloudflare_warp_control_api_host: ClassVar[str] = \
        "api.devices.cloudflare.com"

    # Cloudflare WARP 控制 API 官方 IPv4 地址
    cloudflare_warp_control_api_ipv4_ips: ClassVar[Tuple[str, ...]] = \
        (
            "162.159.137.105",
            "162.159.138.105",
        )
    # Cloudflare WARP  名称
    cloudflare_warp_interface_name: ClassVar[str] = "CloudflareWARP"

    # Https 默认统一端口
    https_port: ClassVar[int] = 443

    # =================== 网络适配配置 ===================
    # 隧道入口, 格式为 IPv4:UDP端口
    warp_tunnel_endpoint: Optional[str] = Field(
        default=None,
        description="Custom MASQUE tunnel endpoint (IPv4:UDP port).",
    )

    # 系统 Socket 选项, 由模型初始化时确定
    # 这些内部属性不参与环境变量解析
    _socket_options: "Optional[SocketOptions]" = PrivateAttr(default=None)

    @model_validator(mode="after")
    def init_socket_options(self) -> Self:
        # Linux
        if sys.platform == "linux":
            self._socket_options = {
                "level": socket.SOL_SOCKET,        # 1
                "optname": socket.SO_BINDTODEVICE, # 25
                "value": self.cloudflare_warp_interface_name.encode("utf-8") \
                    + b'\0',     # WARP 网络接口名称
            }

        # Windows
        elif sys.platform == "win32":
            # ~ 未来更改
            if self.cloudflare_warp_interface_name is None:
                raise ValueError(
                    "WARP interface name must be provided on Windows."
                )

            # 根据手动提供的接口名称获取 Windows 网络接口索引
            interface_index: int = socket.if_nametoindex(
                self.cloudflare_warp_interface_name
            )
            self._socket_options =  {
                "level": socket.IPPROTO_IP, # 0
                "optname": 31,              # IP_UNICAST_IF
                "value": struct.pack("!I", interface_index),
            }

        # MacOS
        elif sys.platform == "darwin":
            raise NotImplementedError(
                f"Unsupported platform: {sys.platform}"
            )
        # Other platforms
        else:
            raise NotImplementedError(                    # pyright: ignore
                f"Unsupported platform: {sys.platform}"
            )
        return self

    @property
    def socket_options(self) -> "SocketOptions":
        if self._socket_options is None:
            raise RuntimeError(
                "Socket options not initialized. "
            )
        return self._socket_options

    model_config = SettingsConfigDict(
        env_file=[".env", ],
        env_file_encoding="utf-8",
        # 额外配置项处理方式
        # - "ignore": 忽略额外配置项
        # - "forbid": 抛出异常
        # - "allow": 允许额外配置项, 并存在 __dict__ 中
        extra="ignore",

        # 是否区分大小写
        case_sensitive=True,
    )

settings = Settings()
