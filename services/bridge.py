from __future__ import annotations
from typing import (
    List,
    Optional,
    TYPE_CHECKING,
)
if TYPE_CHECKING:
    from asyncio import (
        StreamReader,
        StreamWriter,
        Task,
    )

from contextlib import suppress
from ipaddress import ip_address

import socket
import asyncio

from services.network import (
    bind_socket_to_warp_network_interface,
    enable_tcp_socket_keepalive,
    WarpInterfaceUnavailableError,
)
from logger import logger
from config import settings

# Settings
WARP_CONTROL_API_IPV4_IPS \
    = settings.cloudflare_warp_control_api_ipv4_ips           # Cloudflare WARP 控制 API 官方 IPv4 地址列表
BLOCK_IPS = settings.block_ips                                # IP 黑名单, 阻止指定 IP 连接本地 TCP Bridge
CHUNK_SIZE = settings.chunk_size                              # TCP 数据转发缓冲区大小, 单位 bytes
CONNECT_TIMEOUT = settings.connect_timeout                    # TCP 连接 Cloudflare WARP 控制 API 超时时间 (单位: 秒)
HTTPS_PORT = settings.https_port                              # 约定俗成的 HTTPS 统一端口

async def forward(
    reader: "StreamReader",
    writer: "StreamWriter",
):
    # region
    # while True:
    #     data = await reader.read(CHUNK_SIZE)
    #     if not data:
    #         break
    # 等价于于 while data := await reader.read(CHUNK_SIZE):
    # endregion

    # 异步读取 TCP 数据, 返回 bytes
    # 返回 b"" 表示 EOF, 即 源端 不会再发送数据
    while data := await reader.read(CHUNK_SIZE):
        # 将数据写入目标端
        writer.write(data)
        # 等待缓冲区腾出
        await writer.drain()

    # 当 端源 不发送数据时
    if writer.can_write_eof():
        # 发送 EOF 信号, 通知目标端不再有数据发送
        writer.write_eof()
        await writer.drain()

async def get_upstream_connection():
    # 当前运行的事件循环
    running_loop = asyncio.get_running_loop()

    # 尝试 WARP 控制 API 的官方 IPv4 地址
    for i, wrap_ipv4 in enumerate(
        WARP_CONTROL_API_IPV4_IPS,
        start=1
    ):
        # 创建 IPv4 TCP Socket
        upstream_socket = socket.socket(
            socket.AF_INET,      # 2, 使用 IPv4 地址
            socket.SOCK_STREAM   # 1, 使用 TCP 协议
        )
        # Socket 是否已交给 asyncio 接管
        is_socket_handled_by_asyncio = False

        try:
            # 设置为非阻塞模式
            upstream_socket.setblocking(False)

            # 将 Socket 绑定到 WARP 网络接口
            bind_socket_to_warp_network_interface(upstream_socket)
            # 开启 Socket Keepalive, 定期探测连接状态
            enable_tcp_socket_keepalive(upstream_socket)

            try:
                # 在指定事件内, 建立 TCP 连接
                await asyncio.wait_for(
                    running_loop.sock_connect(
                        upstream_socket,
                        (wrap_ipv4, HTTPS_PORT),   # WARP 控制API 官方 IPv4 地址 + HTTPS 默认统一端口(443)
                    ),
                    timeout=CONNECT_TIMEOUT,
                )
            # 捕获 建立 TCP 连接时的 OSError 异常, 包括超时 (TimeoutError)、拒绝连接等
            except OSError as e:
                # 每次失败都记录目标地址.
                logger.warning(
                    "WARP upstream TCP connection attempt failed: "
                    f"upstream={wrap_ipv4}:{HTTPS_PORT}, "
                    f"attempt={i}/{len(WARP_CONTROL_API_IPV4_IPS)}, "
                    f"error={e!r}."  # e!r 输出异常类型和异常信息.
                )
                # 最后一个 IPv4 地址尝试失败, 抛出异常给调用方(raise)
                if i == len(WARP_CONTROL_API_IPV4_IPS):
                    raise

                continue

            # 连接成功
            # 转换 TCP Socket 为 asyncio 的 "StreamReader" 和 "StreamWriter"
            upstream_reader, \
            upstream_writer = \
                await asyncio.open_connection(
                    sock=upstream_socket
                )

            # Socket 已交给 asyncio 接管
            is_socket_handled_by_asyncio = True

            return upstream_reader, upstream_writer
        finally:
            # 如果 Socket 没有交接给 asyncio, 让 Socket 关闭
            if not is_socket_handled_by_asyncio:
                upstream_socket.close()
            # 交接成功, 调用此函数的上层负责关闭, 这里不再关闭
            # 谁拿到了 StreamWriter, 谁就负责关闭 Socket, 这里不再关闭.

    # 没有配置任何 WARP 控制 API IPv4 地址, 抛出异常
    raise RuntimeError(
        "No Cloudflare WARP control API IPv4 addresses configured."
    )

async def handle_tcp_connection(
    client_reader: "StreamReader",
    client_writer: "StreamWriter"
):
    # 获取连接到本地 TCP Bridge 的客户端地址 (IP, port)
    client_peername = client_writer.get_extra_info("peername") # "peername" 是 asyncio 查询的固定字段, 表示客户端的地址信息
    if client_peername is None:
        raise ValueError(
            "WARP client address not available."
        )

    # 将客户端 IP 转换为 IPv4Address 或 IPv6Address
    # 便于检查 in BLOCK_IPS, 否则字符串比较可能出现误判.
    client_ip = ip_address(client_peername[0])
    # 客户端地址拼接
    client_address = ':'.join(map(str, client_peername))
    # 检查客户端 IP 是否在黑名单中
    if BLOCK_IPS is not None and client_ip in BLOCK_IPS:
        logger.warning(
            "WARP TCP bridge rejected: "
            f"client={client_address}. "
            "Client IP is blocked."
        )
        return

    # 上游连接的 StreamWriter
    upstream_writer: Optional["StreamWriter"] = None
    # 并发任务列表
    tasks: List["Task[None]"] = []

    try:
        # 获取 TCP 连接 Cloudflare WARP 控制 API 的 StreamReader, StreamWriter
        upstream_reader, \
        upstream_writer = \
            await get_upstream_connection()

        # 获取上游服务器(WARP 控制 API)的地址信息
        upstream_peername = upstream_writer.get_extra_info("peername")
        # 上游服务器地址
        upstream_address = ':'.join(map(str, upstream_peername))

        # 记录 WARP 客户端连接成功的日志, 包括客户端 IP 和上游服务器地址
        logger.info(
            "WARP TCP bridge connected: "
            f"client={client_address}, "
            f"upstream={upstream_address}."
        )

        # 创建两个并发任务, 分别处理数据转发 (重新赋值)
        tasks = [
            # 本地 WARP 客户端 --> Cloudflare 控制 API
            asyncio.create_task(
                forward(client_reader, upstream_writer)
            ),
            # Cloudflare 控制 API --> 本地 WARP 客户端
            asyncio.create_task(
                forward(upstream_reader, client_writer)
            ),
        ]

        # 任意一个任务抛出异常时立即返回, 但不会取消另一个任务.
        # 否则, 则等到两个任务都正常结束.
        completed_tasks, _ = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_EXCEPTION, # 任意一个任务抛出异常时立即返回
        )
        # 检查已完成任务是否发生异常
        for c_t in completed_tasks:
            c_t.result() # 如果任务发生异常, 这里会抛出异常, 进入 except OSError 分支.

    # get_upstream_connection() 中 bind_socket_to_warp_network_interface() 抛出的异常.
    except WarpInterfaceUnavailableError as e:
        logger.warning(
            # e 异常信息已经包含接口名称和原因, 不重复描述.
            f"WARP TCP bridge rejected: client={client_address}. {e}"
        )

    # 其他 OSError 异常, 由 c_t.result() 抛出.
    except OSError as e:
        # 记录异常信息, 包括客户端地址.
        logger.error(
            f"WARP TCP bridge failed: client={client_address}, "
            f"error={e!r}."  # e!r 输出异常类型和异常信息.
        )

    finally:
        # tasks 中的两个任务, 分别是:
        # WARP 客户端 --> Cloudflare 控制 API 的数据转发任务
        # Cloudflare 控制 API --> WARP 客户端 的数据转发任务
        # 而本函数中的 asyncio.wait_for(..., return_when=asyncio.FIRST_EXCEPTION)
        #  其中一个任务抛出异常时, 不会取消另一个任务, 因此需要手动取消.
        for t in tasks:
            # 如果任务还没有完成
            if not t.done(): # t.donet() 表示任务是否已经完成, 包括正常结束、异常结束和被取消.
                t.cancel()
        # 等待所有任务完成, 并忽略异常
        if tasks:
            # asyncio.gather() 会等待所有任务完成, 并返回一个列表, 包含每个任务的结果.
            # return_exceptions 默认 False. 即如果某个任务抛出异常, 会立即抛出异常.
            await asyncio.gather(
                *tasks,
                return_exceptions=True, # 不抛出异常, 而是作为结果返回
            )

        # 关闭连接上游的 TCP Socket
        # 在 get_upstream_connection() 中 Socket 已经交接给 asyncio,
        # upstream_writer 不为 None.
        # 因此需要关闭 asyncio 的 StreamWriter.
        if upstream_writer is not None:
            upstream_writer.close()
            # 等待上游的 TCP Socket 关闭, 忽略异常.
            # region
            # try:
            #     await upstream_writer.wait_closed()
            # except OSError:
            #     pass
            # 等价于 with suppress(OSError): await upstream_writer.wait_closed()
            # endregion
            with suppress(OSError):
                await upstream_writer.wait_closed()

        # 不负责关闭客户端的 TCP Socket
        # 交由上游负责关闭
        # 谁拿到了 StreamWriter, 谁就负责关闭 Socket, 这里不再关闭.
        # region
        # # 关闭客户端的 TCP Socket
        # client_writer.close()
        # # 等待客户端的 TCP Socket 关闭, 忽略异常.
        # with suppress(OSError):
        #     await client_writer.wait_closed()
        # endregion
