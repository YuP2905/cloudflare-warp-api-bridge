from __future__ import annotations
from typing import (
    List,
    Tuple,
    Optional,
    ClassVar,
    TypedDict,
    TYPE_CHECKING,
)
if TYPE_CHECKING:
    class SocketOptions(TypedDict):
        level: int
        optname: int
        value: bytes

    class KeepaliveOption(TypedDict):
        level: int
        optname: int
        value: int

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
import signal
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
        default=65536,  # 64KB, 65536 字节
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

    # 上游 TCP keepalive: 连接空闲多少秒后开始探测
    # 隧道中途断开时, 用于及时发现并关闭失效的上游连接
    keepalive_idle: int = Field(
        default=30,
        ge=1,
        description="Seconds of idle time before TCP keepalive probes start."
    )
    # 上游 TCP keepalive: 两次探测之间的间隔 (秒)
    keepalive_interval: int = Field(
        default=10,
        ge=1,
        description="Seconds between TCP keepalive probes."
    )
    # 上游 TCP keepalive: 连续多少次探测无响应后判定连接失效
    keepalive_count: int = Field(
        default=3,
        ge=1,
        description="Unanswered TCP keepalive probes before the connection is dropped."
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
    # Cloudflare WARP 网络接口名称
    cloudflare_warp_interface_name: ClassVar[str] = "CloudflareWARP"

    # Https 默认统一端口
    https_port: ClassVar[int] = 443

    # =================== 网络适配配置 ===================
    # 隧道入口, 格式为 IPv4:UDP端口
    warp_tunnel_endpoint: Optional[str] = Field(
        default=None,
        description="Custom MASQUE tunnel endpoint (IPv4:UDP port).",
    )

    # =================== 启停脚本流程配置 ===================
    # 启动脚本等待 warp-svc 可以响应命令的最长时间 (秒)
    warp_svc_wait_timeout: int = Field(
        default=30,
        ge=1,
        description="Seconds the start script waits for warp-svc to respond.",
    )
    # 发出连接命令后, 等待 WARP 真正连上的最长时间 (秒)
    # 超时只记录警告, 不中断服务
    warp_connect_wait_timeout: float = Field(
        default=60.0,
        gt=0,
        description="Seconds to wait for WARP to report Connected.",
    )
    # close 脚本的最长执行时间 (秒), 避免恢复步骤卡住导致进程无法退出
    close_command_timeout: float = Field(
        default=60.0,
        gt=0,
        description="Seconds before the close script is killed.",
    )

    # =================== 平台相关配置 ===================
    # 以下内部属性由模型初始化时按平台确定, 不参与环境变量解析
    # 其他模块只读取对应属性, 不再自行判断平台
    # 将上游 Socket 绑定到 WARP 网络接口的 Socket 选项
    _socket_options: "Optional[SocketOptions]" = PrivateAttr(default=None)
    # 上游 Socket 的 keepalive 选项
    _keepalive_options: "Optional[List[KeepaliveOption]]" = PrivateAttr(default=None)
    # 平台退出信号
    _shutdown_signals: Optional[Tuple[int]] = PrivateAttr(default=None)

    @property
    def socket_options(self):
        if self._socket_options is None:
            raise RuntimeError(
                "Socket options not initialized. "
            )
        return self._socket_options

    @property
    def keepalive_options(self):
        if self._keepalive_options is None:
            raise RuntimeError(
                "Keepalive options not initialized. "
            )
        return self._keepalive_options

    @property
    def shutdown_signals(self):
        if self._shutdown_signals is None:
            raise RuntimeError(
                "Shutdown signals not initialized. "
            )
        return self._shutdown_signals

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

# ================== Linux ===================
if sys.platform == "linux":
    class LinuxSettings(Settings):
        # Linux 平台的设置

        # 运行期间刷新 sudo 授权的间隔 (秒)
        # 必须小于 sudo 授权有效期 (默认 15 分钟), 保证退出时仍有授权
        sudo_refresh_interval: int = Field(
            default=60,
            ge=1,
            description="Seconds between sudo authorization refreshes while running.",
        )

        @model_validator(mode="after")
        def init_platform_options(self):
            # 已发送数据长时间未被确认时的超时 (毫秒)
            user_timeout_ms = (
                self.keepalive_idle
                + self.keepalive_interval * self.keepalive_count
            ) * 1000

            self._socket_options = {
                "level": socket.SOL_SOCKET,              # 1, socket 通用设置
                "optname": socket.SO_BINDTODEVICE,       # 25, 绑定到指定网络接口
                "value": self.cloudflare_warp_interface_name.encode("utf-8") \
                    + b'\0',                             # 指定的网络接口名称 "CloudflareWARP" + b'\0'(0 字节, 字符串结束标记)
            }
            self._keepalive_options = [
                {
                    "level": socket.SOL_SOCKET,          # 1, socket 通用选项
                    "optname": socket.SO_KEEPALIVE,      # 9, 是否启用 keepalive(存活) 探测
                    "value": 1                           # 1, 启用. 0 表示禁用
                },
                {
                    "level": socket.IPPROTO_TCP,         # 6, TCP 协议选项
                    "optname": socket.TCP_KEEPIDLE,      # 4, 空闲多少秒后开始探测
                    "value": self.keepalive_idle         # 连续空闲 self.keepalive_idle 秒后开始探测
                },
                {
                    "level": socket.IPPROTO_TCP,         # 6, TCP 协议选项
                    "optname": socket.TCP_KEEPINTVL,     # 5, 两次探测之间的间隔
                    "value": self.keepalive_interval     # 连续两次探测之间的间隔为 self.keepalive_interval 秒
                },
                {
                    "level": socket.IPPROTO_TCP,         # 6, TCP 协议选项
                    "optname": socket.TCP_KEEPCNT,       # 6, 连续多少次探测无响应后判定连接失效
                    "value": self.keepalive_count        # 连续 self.keepalive_count 次探测无响应后判定连接失效
                },
                {
                    "level": socket.IPPROTO_TCP,         # 6, TCP 协议选项
                    "optname": socket.TCP_USER_TIMEOUT,  # 18, 已发送数据长时间未被确认时的超时
                    "value": user_timeout_ms             # 超时为 user_timeout_ms 毫秒
                },
            ]
            # 关闭终端窗口
            self._shutdown_signals = (
                signal.SIGHUP,  # signal 1, 终端关闭
            )
            return self

    settings = LinuxSettings()

# =================== Windows ===================
# ~ Furture Work (未来计划, 仅针对 win11 22H2 及以上版本, 其他版本不打算测试和支持)
elif sys.platform == "win32":
    class WindowsSettings(Settings):
        # Windows 平台的设置

        @model_validator(mode="after")
        def init_platform_options(self):
            # 根据接口名称获取 Windows 网络接口索引
            interface_index: int = socket.if_nametoindex(
                self.cloudflare_warp_interface_name
            )
            self._socket_options =  {
                "level": socket.IPPROTO_IP,                  # 0
                "optname": 31,                               # IP_UNICAST_IF
                "value": struct.pack("!I", interface_index),
            }
            self._keepalive_options = [
                {
                    "level": socket.SOL_SOCKET,             # 1, socket 通用选项
                    "optname": socket.SO_KEEPALIVE,         # 9, socket 保活选项
                    "value": 1                              # 1, 启用. 0 表示禁用
                },
                {
                    "level": socket.IPPROTO_TCP,            # 6, TCP 协议选项
                    "optname": socket.TCP_KEEPIDLE,         # 4, 空闲多少秒后开始探测
                    "value": self.keepalive_idle            # 连续空闲 self.keepalive_idle 秒后开始探测
                },
                {
                    "level": socket.IPPROTO_TCP,            # 6, TCP 协议选项
                    "optname": socket.TCP_KEEPINTVL,        # 5, 两次探测之间的间隔
                    "value": self.keepalive_interval        # 连续两次探测之间的间隔为 self.keepalive_interval 秒
                },
                {
                    "level": socket.IPPROTO_TCP,            # 6, TCP 协议选项
                    "optname": socket.TCP_KEEPCNT,          # 6, 连续多少次探测无响应后判定连接失效
                    "value": self.keepalive_count           # 连续 self.keepalive_count 次探测无响应后判定连接失效
                },
            ]
            # 控制台 Ctrl+Break
            self._shutdown_signals = (
                signal.SIGBREAK, # signal 21, 控制台 Ctrl+Break
            )
            return self

    settings = WindowsSettings()

# =================== macOS ===================
# ! Not Gonna Work (不打算支持, 仅用于抛出异常提示)
elif sys.platform == "darwin":
    raise NotImplementedError(
        "I'm not gonna support MacOS. Because I don't like f**king closed systems like MacOS."
    )
# =================== 其他平台 ===================
# ! 本人孤陋寡闻, 仅知道 windows 和 linux (仅用于抛出异常提示, 更不会支持了)
else:
    raise NotImplementedError(
        f"Forgive my ignorance, I really don't know what platform '{sys.platform}' is, "
        "and I'm not gonna support it."
    )
