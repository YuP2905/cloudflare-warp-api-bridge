from __future__ import annotations
from typing import (
    Dict,
    TYPE_CHECKING,
)
if TYPE_CHECKING:
    from asyncio import (
        StreamReader,
        StreamWriter,
        Task,
    )

import asyncio

from logger import logger
from services.bridge import (
    handle_tcp_connection,
)

class TCPConnections:
    def __init__(self):
        # 初始化客户端连接: { 正在转发任务: 对应客户端的 StreamWriter }
        self._connections: Dict["Task[None]", "StreamWriter"] = {}
        # 初始化关闭标志
        # self.close() 方法设置为 True
        self._closing = False

    def accept(
        self,
        client_reader: "StreamReader",
        client_writer: "StreamWriter",
    ):
        # 如果正在关闭, 不 create_task, 直接关闭客户端连接
        if self._closing:
            client_writer.close()
            return

        # 创建转发任务 (只是排队, 需要事件循环调度, 才会执行)
        task = asyncio.create_task(
            handle_tcp_connection(client_reader, client_writer)
        )
        # 记录任务和对应的客户端 StreamWriter
        self._connections[task] = client_writer

        # 任务完成(正常结束/抛异常/被取消), 调用 _discard 方法清理记录
        task.add_done_callback(self._discard)

    def _discard(
        self,
        task: "Task[None]",
    ):
        # 移除任务和对应的客户端 StreamWriter
        # 并得到移除的客户端 StreamWriter
        client_writer = self._connections.pop(task)
        # 获取客户端 HOST 和 PORT
        client_peername = client_writer.get_extra_info("peername")
        client_address = ':'.join(map(str, client_peername)) \
            if client_peername is not None else "unknown"

        # 关闭客户端的 TCP Socket
        client_writer.close()

        # 如果任务没有被取消
        if not task.cancelled():
            # 获取任务的异常信息
            error = task.exception()
            if error is not None:
                logger.error(
                    "WARP TCP bridge task failed: "
                    f"client={client_address}.",
                    exc_info=error, # 打印异常堆栈
                )

    async def close(self):
        # 设置关闭为 True
        self._closing = True

        # 拷贝当前的连接任务列表, 避免 self._discard 在取消任务时修改 self._connections
        pending_tasks = tuple(self._connections)
        # 取消所有正在转发的任务
        for p_t in pending_tasks:
            p_t.cancel()

        # 等待所有任务完成, 忽略异常
        if pending_tasks:
            await asyncio.gather(
                *pending_tasks,
                return_exceptions=True,
            )
