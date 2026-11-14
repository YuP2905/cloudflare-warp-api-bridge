from __future__ import annotations
from typing import (
    List,
    Optional,
    Tuple,
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
from uvicorn.config import logger

import socket
import asyncio

from services.network import (
    WarpInterfaceUnavailableError,
    bind_warp_interface,
    enable_keepalive,
)
from config import settings

# Settings
CLOUDFLARE_WARP_CONTROL_API_IPV4_IPS \
    = settings.cloudflare_warp_control_api_ipv4_ips
BLOCK_IPS = settings.block_ips
CHUNK_SIZE = settings.chunk_size
CONNECT_TIMEOUT = settings.connect_timeout
# HTTPS 默认统一端口
HTTPS_PORT = settings.https_port

async def forward(
    reader: "StreamReader",
    writer: "StreamWriter",
) -> None:
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

    # 当 端源 不发送数据时, 是否向 目标端 发送 EOF 信号
    if writer.can_write_eof():
        # 发送 EOF 信号, 通知目标端不再有数据发送
        writer.write_eof()
        await writer.drain()

async def get_upstream_connection(
) -> Tuple["StreamReader", "StreamWriter"]:
    error: Optional[OSError] = None
    loop = asyncio.get_running_loop()

    # 依次尝试 Cloudflare WARP 控制 API 的官方 IPv4 地址
    for c_ip4 in CLOUDFLARE_WARP_CONTROL_API_IPV4_IPS:
        # 创建 TCP Socket
        # AF_INET: 使用 IPv4; SOCK_STREAM: 使用 TCP 协议
        upstream_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Socket 是否已交给 asyncio 接管, 未接管时在 finally 中关闭
        handed_over = False

        try:
            # 将 Socket 绑定到 WARP 网络接口
            # 接口不存在时抛出 WarpInterfaceUnavailableError
            # 所有官方地址都走同一个接口, 因此不再尝试其他地址, 直接交给调用方
            bind_warp_interface(upstream_socket)

            # 开启 keepalive, 隧道断开时及时发现失效连接
            enable_keepalive(upstream_socket)

            # 设置为非阻塞模式
            upstream_socket.setblocking(False)

            try:
                # 等待连接成功或超时
                await asyncio.wait_for(
                    loop.sock_connect(
                        upstream_socket,
                        (c_ip4, HTTPS_PORT),
                    ),
                    timeout=CONNECT_TIMEOUT,
                )
            # OSError 是 socket 连接失败时抛出的异常, 包括超时 (TimeoutError)、拒绝连接等
            except OSError as e:
                error = e
                # 当前 IP 连接失败后, 继续尝试下一个官方地址
                logger.warning(
                    f"Failed to connect to Cloudflare API at "
                    f"{c_ip4}:{HTTPS_PORT}: {type(e).__name__}: {e}"
                )
                continue

            # 转换 TCP socket 为 asyncio 的 StreamReader 和 StreamWriter
            upstream_reader, upstream_writer = \
                await asyncio.open_connection(
                    sock=upstream_socket
                )
            handed_over = True

            # 连接成功, 记录日志
            logger.info(
                f"Cloudflare API TCP connected: {c_ip4}:{HTTPS_PORT}",
            )

            # 连接成功, 返回 StreamReader 和 StreamWriter
            return upstream_reader, upstream_writer

        finally:
            # 释放未被 asyncio 接管的 Socket
            if not handed_over:
                upstream_socket.close()

    if error is not None:
        raise error

    raise RuntimeError(
        "No Cloudflare WARP control API IPv4 addresses configured."
    )

async def handle_tcp_connection(
    client_reader: "StreamReader",
    client_writer: "StreamWriter",
) -> None:
    upstream_writer: Optional["StreamWriter"] = None
    tasks: List["Task[None]"] = []

    try:
        # 获取连接到本地 TCP Bridge 的客户端地址
        # "peername" 是 asyncio 查询的固定字段
        client_peername = client_writer.get_extra_info("peername")

        if client_peername is None:
            raise ValueError(
                "WARP client address not available."
            )

        # 将客户端 IP 转换为 IPv4Address 或 IPv6Address
        # 与 Settings 中的 IP 黑名单比较
        client_ip = ip_address(client_peername[0])

        if BLOCK_IPS is not None and client_ip in BLOCK_IPS:
            logger.warning(
                f"WARP client blocked: {client_ip}",
            )
            return

        # 创建第二条 TCP 连接, 连接 Cloudflare WARP 控制 API
        upstream_reader, upstream_writer = \
            await get_upstream_connection()

        # 创建两个并发任务
        tasks = [
            # 本地 WARP 客户端 --> Cloudflare 控制 API
            # 发送数据
            asyncio.create_task(
                forward(client_reader, upstream_writer)
            ),

            # Cloudflare 控制 API --> 本地 WARP 客户端
            # 接收数据
            asyncio.create_task(
                forward(upstream_reader, client_writer)
            ),
        ]
        # FIRST_EXCEPTION: 任意一个任务抛出异常时立即返回
        # 如果没有任务抛出异常, 则等到两个任务都正常结束 (等同于 ALL_COMPLETED)
        # 因此一个方向先读到 EOF 时, 另一个方向会继续转发, 支持 TCP 半关闭
        completed, _ = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_EXCEPTION,
        )

        # 检查已完成任务是否发生异常
        for c_t in completed:
            c_t.result() # 如果任务发生异常, 这里会抛出异常, 进入 except 块

    except WarpInterfaceUnavailableError as e:
        # WARP 隧道尚未建立或正在重连, 属于预期情况
        # 关闭本次连接即可, warp-svc 会自行重试
        logger.warning(
            f"WARP interface not ready, API connection dropped: {e.strerror}"
        )

    except OSError as e:
        # 网络或系统调用失败, 记录错误并关闭当前 TCP 连接
        logger.error(
            f"WARP TCP bridge connection error: "
            f"{type(e).__name__}: {e!r}"
        )

    except Exception:
        # 非预期错误, 在这里输出一次完整 Traceback
        # 不再向上抛出, 避免在任务回调中重复记录
        logger.exception("Unexpected TCP bridge error.")

    finally:
        for t in tasks:
            # 如果任务还没有完成, 取消
            if not t.done():
                t.cancel()

        if tasks:
            # 等待所有任务完成, 并忽略异常
            await asyncio.gather(
                *tasks,
                # 异常不抛出
                return_exceptions=True,
            )

        # 关闭连接 Cloudflare WARP 控制 API 的 TCP Socket
        if upstream_writer is not None:
            upstream_writer.close()

            # 内部抛出 OSError 时, 可能是因为连接已经关闭, 这里忽略异常
            with suppress(OSError):
                await upstream_writer.wait_closed()

        # 关闭本地 WARP 客户端的 TCP Socket
        client_writer.close()

        with suppress(OSError):
            await client_writer.wait_closed()
