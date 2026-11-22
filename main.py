from __future__ import annotations
from typing import (
    List,
    Callable,
    Optional,
    TYPE_CHECKING,
)
if TYPE_CHECKING:
    from asyncio import (
        StreamReader,
    )

from contextlib import (
    suppress,
)
from pathlib import Path

import asyncio
import signal
import subprocess
import sys

# logger
from logger import (
    logger,
)
# services
from services.tcp import (
    TCPConnections,
)

# Settings
from config import (
    settings,
)

TCP_HOST = settings.tcp_host                                         # TCP 服务监听的 HOST,
TCP_PORT = settings.tcp_port                                         # TCP 服务监听的 PORT
HTTPS_PORT = settings.https_port                                     # 约定俗成的 HTTPS 统一端口
WARP_SERVICE_NAME = settings.warp_service_name                       # WARP 后台服务名称
WARP_TUNNEL_ENDPOINT = settings.warp_tunnel_endpoint                 # WARP Tunnel 隧道入口

WARP_SVC_WAIT_TIMEOUT = settings.warp_svc_wait_timeout               # WARP 服务启动等待超时时间 (单位: 秒)
WARP_CONNECT_WAIT_TIMEOUT = settings.warp_connect_wait_timeout       # WARP 连接等待超时时间 (单位: 秒)
CLOSE_COMMAND_TIMEOUT = settings.close_command_timeout               # 关闭命令超时时间 (单位: 秒)

# 平台退出信号
# - SIGHUP: 1, 终端关闭 (Linux)
# - SIGBREAK: 21, 控制台 Ctrl+Break (Windows)
# - SIGINT: 2, Ctrl+C
# - SIGTERM: 15, kill <pid>
SHUTDOWN_SIGNALS = settings.shutdown_signals                         # Linux: (SIGHUP, )  Windows: (SIGBREAK, )

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent
# 共用脚本路径
SHARED_COMMAND_PARENT_DIR_PATH = PROJECT_ROOT / "command"
# 共用脚本参数
SHARED_COMMAND_ARGS = [
    TCP_HOST,                       # $1
    str(TCP_PORT),                  # $2
    str(HTTPS_PORT),                # $3
    WARP_TUNNEL_ENDPOINT or "",     # $4
    str(WARP_SVC_WAIT_TIMEOUT),     # $5
    WARP_SERVICE_NAME,              # $6
]

LOG_MESSAGE = settings.log_message  # 日志消息

async def log_process_output(
    stream: Optional["StreamReader"],
    log: Callable[[str], None],
):
    # 逐行读取子进程的输出, 交给 logger 输出, 和项目日志格式一致
    if stream is None:
        return
    while line := await stream.readline():
        log(line.decode("utf-8", errors="replace").rstrip())

async def run_process(
    command: List[str],
    timeout: Optional[float] = None,
):
    # 运行子进程 (启停脚本, warp-cli), 输出交给 logger, 返回退出码
    # 超时时结束子进程, 抛出 TimeoutError
    if sys.platform == "win32":
        # 在新的进程组中运行: 控制台的 Ctrl+C 不会发给子进程,
        # cmd.exe 不会弹出 "终止批处理操作吗(Y/N)?", 脚本也不会被中断
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        creationflags = 0

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,     # 捕获标准输出, 交给 logger.info
        stderr=asyncio.subprocess.PIPE,     # 捕获标准错误输出, 交给 logger.error
        creationflags=creationflags,
    )
    # 在后台逐行读取子进程的输出
    output_logging = asyncio.gather(
        log_process_output(process.stdout, logger.info),
        log_process_output(process.stderr, logger.error),
    )
    try:
        return await asyncio.wait_for(
            process.wait(),
            timeout
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    except asyncio.CancelledError:
        # Ctrl+C 取消 main() 时, Windows 上的子进程在新的进程组里, 不会被中断
        # 等它执行完再继续退出, 避免 start 脚本还在修改 WARP 设置时, close 脚本就开始恢复
        await process.wait()
        raise
    finally:
        # 等子进程的输出全部交给 logger
        await output_logging

async def run_command(
    script_name: str,
    timeout: Optional[float] = None,
):
    COMMAND_DIR_PATH = SHARED_COMMAND_PARENT_DIR_PATH / sys.platform
    # Linux 平台使用 bash 脚本
    if sys.platform == "linux":
        command_file = COMMAND_DIR_PATH / f"{script_name}.sh"
        SUDO_REFRESH_INTERVAL = settings.sudo_refresh_interval
        command = [
            "bash",
            str(command_file),
            *SHARED_COMMAND_ARGS,
            # Linux 专属
            str(SUDO_REFRESH_INTERVAL),     # $7
        ]
    # Windows 平台使用 bat 脚本
    else: # setiings 当中其他平台直接 raise NotImplementedError, 这里直接 else
        command_file = COMMAND_DIR_PATH / f"{script_name}.bat"
        command = [
            str(command_file),
            *SHARED_COMMAND_ARGS,
        ]

    # 启动脚本, 输出交给 logger
    try:
        return_code = await run_process(
            command,
            timeout
        )
    except TimeoutError:
        raise RuntimeError(
            f"Command '{script_name}' timed out after {timeout}s."
        )

    if return_code != 0:
        raise RuntimeError(
            f"Failed to run command '{script_name}' with return code {return_code}."
        )

async def connect_warp_command():
    # 当 warp-cli connect 命令返回非 0 时 (输出交给 logger)
    if await run_process(["warp-cli", "connect"]) != 0:
        raise RuntimeError(
            "Failed to connect WARP."
        )

async def wait_warp_connected():
    # 当前事件循环
    loop = asyncio.get_running_loop()
    # 计算截止时间戳
    deadline = loop.time() + WARP_CONNECT_WAIT_TIMEOUT

    # 当前事件小于截止时间, 循环检查 WARP 连接状态
    while loop.time() < deadline:
        process = await asyncio.create_subprocess_exec(
            "warp-cli",
            "status",
            stdout=asyncio.subprocess.PIPE,      # 捕获标准输出
            stderr=asyncio.subprocess.DEVNULL,   # 忽略标准错误输出
        )
        # 等待进程结束并获取输出
        stdout, _ = await process.communicate()

        # 检查输出中是否包含 "Status update: Connected"
        warp_connected_msg = b"Status update: Connected"
        if warp_connected_msg in stdout:
            logger.info("WARP connected.")
            return
        # 如果没有连接成功, 等待 1 秒后再次检查
        await asyncio.sleep(1)

    logger.warning(
        f"WARP is not connected after {WARP_CONNECT_WAIT_TIMEOUT:.0f}s, "
        f"check `warp-cli status`."
    )

async def serve_tcp_bridge():
    # 启动 TCP 服务, 接收客户端连接
    tcp_connections = TCPConnections()
    tcp_server = await asyncio.start_server(
        tcp_connections.accept,
        host=str(TCP_HOST),
        port=TCP_PORT,
    )
    # 打印日志
    logger.info(
        f"WARP TCP bridge starting to listen on {TCP_HOST}:{TCP_PORT}"
    )

    try:
        # 启动 WARP 连接
        await connect_warp_command()
        # 等待 WARP 连接成功
        # 不再需要 yield, 所以直接 await, 不用后台任务 + cancel
        await wait_warp_connected()

        # 一直等待, 直到 Ctrl+C 取消 main(), 相当于原来 lifespan 的 yield
        # 这个事件永远不会被 set, 所以会一直等到被取消
        # 不用 tcp_server.serve_forever(): 它被取消时会先等所有连接自己断开,
        # 而 warp-svc 的长连接不会自己断, 退出会卡住
        await asyncio.Event().wait()
    finally:
        # 关闭 TCP 服务, 停止接收客户端连接
        tcp_server.close()
        logger.info("TCP bridge shutting down...")

        # 关闭所有正在转发的 TCP 连接, 等待任务完成
        await tcp_connections.close()
        await tcp_server.wait_closed()

async def main():
    if LOG_MESSAGE:
        # 打印日志消息
        for msg in LOG_MESSAGE:
            logger.info(msg)

    try:
        # 启动设置脚本
        await run_command("start")
        # 运行 TCP 桥接服务, 直到收到退出信号
        await serve_tcp_bridge()
    finally:
        for e_s in (
            signal.SIGINT,     # Ctrl+C
            signal.SIGTERM,    # kill <pid>
            *SHUTDOWN_SIGNALS  # 平台退出信号
        ):
            # 忽略退出信号, 避免再次 Ctrl+C, kill <pid> 等无法退出
            signal.signal(
                e_s,
                signal.SIG_IGN   # 忽略退出信号
            )

        # 启动关闭设置脚本
        await run_command(
            "close",
            timeout=CLOSE_COMMAND_TIMEOUT
        )

if __name__ == "__main__":
    # signal.signal 类似 JS 的 addEventListener("", () => {}) 监听事件
    for s_signal in (
        signal.SIGTERM,     # 15, kill <pid>
        *SHUTDOWN_SIGNALS   # 平台退出信号
    ):
        # 收到这些退出信号时, 手动触发 SIGINT, 等同于按下 Ctrl+C
        # 因为 asyncio.run 只处理 SIGINT: 取消 main(), 执行 finally 分支
        signal.signal(
            s_signal,
            # signal.raise_signal 类似 JS 的 target.dispatchEvent(event) 手动触发事件
            lambda _s, _f: signal.raise_signal(signal.SIGINT) # Ctrl+C
        )

    # 按下 Ctrl+C 后, asyncio.run 会先取消 main() 并执行完 finally 分支,
    # 最后抛出 KeyboardInterrupt, 这里忽略它, 正常退出
    with suppress(KeyboardInterrupt):
        try:
            asyncio.run(main())
        except Exception:
            # 其他异常 (例如启动脚本失败) 也交给 logger 输出, 和其他日志格式一致, 并附带异常堆栈
            logger.exception("WARP TCP bridge stopped with an error.")
            sys.exit(1)
