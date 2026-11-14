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

from uvicorn.config import logger

import asyncio

from services.bridge import (
    handle_tcp_connection,
)

class TCPConnections:
    def __init__(self) -> None:
        self._connections: Dict["Task[None]", "StreamWriter"] = {}
        # 记录是否正在关闭, 避免在关闭过程中接入新的 TCP 客户端
        self._closing = False

    def accept(
        self,
        client_reader: "StreamReader",
        client_writer: "StreamWriter",
    ) -> None:
        if self._closing:
            client_writer.close()
            return
        task = asyncio.create_task(
            handle_tcp_connection(client_reader, client_writer)
        )
        self._connections[task] = client_writer
        task.add_done_callback(self._discard)

    def _discard(
        self,
        task: "Task[None]",
    ) -> None:
        client_writer = self._connections.pop(task)

        # 任务可能在首次执行前被取消, 未进入处理函数的 finally
        # 因此这里确保客户端连接关闭, 重复 close 是允许的
        client_writer.close()

        # 处理函数会自行记录所有 Exception, 这里只兜底意外逃逸的异常
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.error("TCP connection task failed.", exc_info=error)

    async def close(self) -> None:
        self._closing = True

        # 完成回调会移除任务, 使用快照固定本次需要等待的对象
        pending = tuple(self._connections)
        for task in pending:
            task.cancel()

        if pending:
            await asyncio.gather(
                *pending,
                return_exceptions=True,
            )
