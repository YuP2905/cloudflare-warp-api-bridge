from __future__ import annotations
from typing import (
    List,
    Tuple,
    Optional,
    ClassVar,
    TypedDict,
    cast,
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

if sys.platform == "win32":
    import errno
    import ctypes
    import struct

    # Windows 自带的 iplhpapi.dll, 用于查询网络接口信息
    IPHLPAPI = ctypes.WinDLL("iphlpapi")

    def get_interface_index(
        interface_name: str,
    ):
        # 先声明两个 ctypes 变量, 用于接收函数返回值
        luid = ctypes.c_uint64()          #  Windows 网卡内部 ID, 8 字节无符号整型
        interface_index = ctypes.c_ulong() # Windows 网卡编号, 4 字节无符号整型

        # 网卡名称 ---> 网卡内部 ID
        # 0 表示成功, 87 表示参数无效
        alias_to_luid_status = IPHLPAPI.ConvertInterfaceAliasToLuid(
            ctypes.c_wchar_p(interface_name),      # 输入参数: 网络接口名
            ctypes.byref(luid)                     # 输出参数: Windows 网卡内部 ID
        )
        if alias_to_luid_status != 0:
            raise OSError(
                errno.ENODEV,    # 19, 设备不存在
                f"WARP interface '{interface_name}' not found."
            )

        # 网卡内部 ID ---> 网卡编号
        # 0 表示成功, 2 表示找不到指定的网卡
        luid_to_index_status = IPHLPAPI.ConvertInterfaceLuidToIndex(
            ctypes.byref(luid),                    # 输入参数: Windows 网卡内部 ID
            ctypes.byref(interface_index)          # 输出参数: 接收 Windows 网卡编号
        )
        if luid_to_index_status != 0:
            raise OSError(
                errno.ENODEV,    # 19, 设备不存在
                f"WARP interface '{interface_name}' disappeared while getting its index."
            )

        return interface_index.value

class Settings(BaseSettings):
    # =================== 模式配置 ===================
    dev_mode: bool = Field(
        default=True,
        description="Development mode"
    )

    # =================== 本地 TCP 桥接服务配置 ===================
    # 可选(即具备默认值)的配置项
    # 本地 TCP 桥接服务的监听地址
    tcp_host: str = Field(
        default="127.0.0.1",
        description="TCP host."
    )
    # 本地 TCP 桥接服务的监听端口
    # Linux 监听 443 时需要 root 权限, 监听其他端口时需要 iptables 转发
    # Windows 没有特权端口限制, 直接监听 443. 会在 model_validator(mode="after") 中强制设置为 443.
    tcp_port: int = Field(
        default=8843,
        ge = 1,
        le = 65535,
        description="TCP port."
    )

    # 客户端 IP 地址黑名单
    block_ips: Optional[List[IPvAnyAddress]] = Field(
        default=None,
        description="List of blocked IP addresses."
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
    # 运行期间刷新 sudo 授权的间隔 (秒)
    # 必须小于 sudo 授权有效期 (默认 15 分钟), 保证退出时仍有授权
    # Windows 没有 sudo 授权, 该配置项会在 model_validator(mode="after") 中被设置为 None
    sudo_refresh_interval: Optional[int] = Field(
        default=60,
        ge=1,
        le=900, # sudo 授权有效期默认 15 分钟, 900 秒
        description="Seconds between sudo authorization refreshes while running.",
    )

    # 上游 Socket 的 keepalive 选项
    _keepalive_options: "Optional[List[KeepaliveOption]]" = PrivateAttr(default=None)
    # 平台退出信号
    _shutdown_signals: Optional[Tuple[int]] = PrivateAttr(default=None)
    # WARP 后台服务名称, 启动脚本启动服务
    _warp_service_name: Optional[str] = PrivateAttr(default=None)
    # 日志消息缓存, 用于在日志系统初始化前缓存日志消息
    _log_message: List[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def init_platform_options(self):
        # 通用 keepalive 选项
        keepalive_options: "List[KeepaliveOption]" = [
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
        # =================== Linux ===================
        if sys.platform == "linux":
            # 设置了特权端口(即 1024 以下端口)
            if self.tcp_port < 1024:
                # Linux 监听 443 时需要 root 权限, 监听其他端口时需要 iptables 转发
                raise ValueError(
                    "On Linux, listening on ports below 1024 requires root privileges. "
                    "Maybe you can use `sudo` to run this program, but I don't recommend it. "
                    "Please use a port above 1024, and I already set up iptables to forward port 443 to a non-privileged port."
                )

            # WARP 后台服务名称: systemd 服务 warp-svc
            self._warp_service_name = "warp-svc"

            # 已发送数据长时间未被确认时的超时 (毫秒)
            user_timeout_ms = (
                self.keepalive_idle
                + self.keepalive_interval * self.keepalive_count
            ) * 1000
            # Linux 的 TCP_USER_TIMEOUT 选项用于设置已发送数据长时间未被确认时的超时, 单位为毫秒
            keepalive_options.append(
                {
                    "level": socket.IPPROTO_TCP,         # 6, TCP 协议选项
                    "optname": socket.TCP_USER_TIMEOUT,  # 18, 已发送数据长时间未被确认时的超时
                    "value": user_timeout_ms             # 超时为 user_timeout_ms 毫秒
                }
            )
            self._keepalive_options = keepalive_options

            # 关闭终端窗口
            self._shutdown_signals = (
                signal.SIGHUP,  # signal 1, 终端关闭
            )
        elif sys.platform == "win32":
            # 当 tcp_port 不为 443 时 (不管是显式设置还是默认值), 提示
            if self.tcp_port != 443:
                # Windows 没有特权端口(即 1024 以下端口)的限制, 任何程序都能监听 443, 无需转发.
                self._log_message.append(
                    "On Windows, there is no privileged port restriction, so the tcp_port is forcibly set to 443."
                )
            # 当 sudo_refresh_interval 不为 None 时, 提示
            if self.sudo_refresh_interval is not None:
                # Windows 没有 sudo 授权
                self._log_message.append(
                    "On Windows, there is no sudo authorization, so the sudo_refresh_interval is forcibly set to None."
                )

            # 不管是显式设置还是默认值, 强制设置为 None
            self.sudo_refresh_interval = None
            self.tcp_port = self.https_port # 443

            # WARP 后台服务名称: Windows 服务 CloudflareWARP (进程是 warp-svc.exe)
            # 和网卡名称 cloudflare_warp_interface_name 恰好同名
            self._warp_service_name = "CloudflareWARP"

            self._keepalive_options = keepalive_options
            self._shutdown_signals = (
                signal.SIGBREAK, # signal 21, 控制台 Ctrl+Break
            )
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

        return self

    @property
    def keepalive_options(self):
        if self._keepalive_options is None:
            raise RuntimeError(
                "Keepalive options not initialized. "
            )
        return self._keepalive_options

    @property
    def warp_service_name(self):
        if self._warp_service_name is None:
            raise RuntimeError(
                "WARP service name not initialized. "
            )
        return self._warp_service_name

    @property
    def shutdown_signals(self):
        if self._shutdown_signals is None:
            raise RuntimeError(
                "Shutdown signals not initialized. "
            )
        return self._shutdown_signals

    @property
    def log_message(self):
        return self._log_message

    def get_socket_options(self):
        # 每次绑定上游 Socket 时调用, 返回绑定 WARP 网络接口的 Socket 选项
        # 用方法而不是 property: Windows 每次都要调用系统函数查网卡编号, 网卡不存在时会抛出 OSError
        if sys.platform == "linux":
            # 设置 Socket 选项
            # Linux 的 SO_BINDTODEVICE 直接使用网络接口名称, 内容固定不变
            # 由内核在每次 setsockopt 时查找网络接口, 不存在时抛出 ENODEV
            socket_options = {
                "level": socket.SOL_SOCKET,              # 1, socket 通用设置
                "optname": socket.SO_BINDTODEVICE,       # 25, 绑定到指定网络接口
                "value": self.cloudflare_warp_interface_name.encode("utf-8") \
                    + b'\0',                             # 指定的网络接口名称 "CloudflareWARP" + b'\0'(0 字节, 字符串结束标记)
            }
        else:
            # Windows (其他平台在 init_platform_options 中已经抛出异常, 不会走到这里)
            # 设置 Socket 选项
            # Windows 的 IP_UNICAST_IF 需要网卡编号, 不能直接使用网络接口名称
            # 也不能使用 socket.if_nametoindex, 因为 Windows 上只认 "ethernet_0" 这类内部名称不能直接使用网络接口名称
            interface_index = get_interface_index(
                self.cloudflare_warp_interface_name
            )
            socket_options = {
                "level": socket.IPPROTO_IP,                      # 0, IP 协议选项
                "optname": 31,                                   # IP_UNICAST_IF, 绑定到指定网络接口
                "value": struct.pack("!I", interface_index),     # 将网络接口索引转换为 4 字节的网络字节序 bytes
            }
        return cast("SocketOptions", socket_options)

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
