from __future__ import annotations
from typing import (
    Callable,
    Awaitable,
    Optional,
    TYPE_CHECKING,
)
if TYPE_CHECKING:
    from asyncio import (
        Task,
    )

from contextlib import (
    asynccontextmanager,
    suppress,
)
from fastapi import (
    FastAPI,
    Request,
    status,
)
from fastapi.responses import (
    Response,
    JSONResponse,
)
from uvicorn.config import logger
from ipaddress import ip_address

from pathlib import Path
import asyncio
import sys

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
BLOCK_IPS = settings.block_ips                                       # IP 黑名单
HTTPS_PORT = settings.https_port                                     # 约定俗成的 HTTPS 统一端口
DEV_MODE = settings.dev_mode                                         # 开发模式
WARP_TUNNEL_ENDPOINT = settings.warp_tunnel_endpoint                 # WARP Tunnel 隧道入口

WARP_SVC_WAIT_TIMEOUT = settings.warp_svc_wait_timeout               # WARP 服务启动等待超时时间 (单位: 秒)
WARP_CONNECT_WAIT_TIMEOUT = settings.warp_connect_wait_timeout       # WARP 连接等待超时时间 (单位: 秒)
CLOSE_COMMAND_TIMEOUT = settings.close_command_timeout               # 关闭命令超时时间 (单位: 秒)

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent
# 共用脚本路径
SHARED_COMMAND_DIR_PATH = PROJECT_ROOT / "command"
# 共用脚本参数
SHARED_COMMAND_ARGS = [
    TCP_HOST,                       # $1
    str(TCP_PORT),                  # $2
    str(HTTPS_PORT),                # $3
    WARP_TUNNEL_ENDPOINT or "",     # $4
    str(WARP_SVC_WAIT_TIMEOUT),     # $5
]

async def run_command(
    script_name: str,
    timeout: Optional[float] = None,
):
    # Linux 平台使用 bash 脚本
    if sys.platform == "linux":
        command_file = SHARED_COMMAND_DIR_PATH / sys.platform / f"{script_name}.sh"
        SUDO_REFRESH_INTERVAL = settings.sudo_refresh_interval
        command = [
            "bash",
            str(command_file),
            *SHARED_COMMAND_ARGS,
            # Linux 专属
            str(SUDO_REFRESH_INTERVAL),     # $6
        ]
    # ~ Future Work
    # Windows 平台使用 bat 脚本
    elif sys.platform == "win32":
        command_file = SHARED_COMMAND_DIR_PATH / sys.platform / f"{script_name}.bat"
        command = [
            "cmd.exe",
            "/c",
            str(command_file),
            *SHARED_COMMAND_ARGS,
        ]
    # 其余平台不支持
    else:
        raise NotImplementedError(
            f"Unsupported platform: {sys.platform}"
        )

    # 启动脚本
    process = await asyncio.create_subprocess_exec(*command)
    try:
        return_code = await asyncio.wait_for(
            process.wait(),
            timeout
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(
            f"Command '{script_name}' timed out after {timeout}s."
        )

    if return_code != 0:
        raise RuntimeError(
            f"Failed to run command '{script_name}' with return code {return_code}."
        )

async def connect_warp_command():
    process = await asyncio.create_subprocess_exec(
        "warp-cli",
        "connect",
    )

    # 当 warp-cli connect 命令返回非 0 时
    if await process.wait() != 0:
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

@asynccontextmanager
async def lifespan(
    app: FastAPI,
):
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

    # 初始一个 WARP 连接等待任务
    wait_warp_connect_task: Optional["Task[None]"] = None
    try:
        # yield 前, 启动服务, 类似 Vue 的 onBeforeMount.
        # uvicorn 会完成在 yield 前的逻辑, 才开始监听 HTTP

        # 启动 WARP 连接, 并等待连接成功
        await connect_warp_command()
        wait_warp_connect_task = asyncio.create_task(
            wait_warp_connected()
        )
        yield  # 服务运行中, 并不类似 Vue 的生命周期钩子
        # yield 后, 关闭服务, 类似 Vue 的 onBeforeUnmount
        # uvicorn 已经停止接收 HTTP
    finally:
        # 当服务关闭时, 取消 WARP 连接等待任务, 避免阻塞退出
        if wait_warp_connect_task is not None \
            and not wait_warp_connect_task.done():
            # 取消等待 WARP 连接的任务
            wait_warp_connect_task.cancel()
            # 等待任务完成, 忽略 asyncios 的 CancelledError 异常
            with suppress(asyncio.CancelledError):
                await wait_warp_connect_task

        # 关闭 TCP 服务, 停止接收客户端连接
        tcp_server.close()
        logger.info("TCP bridge shutting down...")

        # 关闭所有正在转发的 TCP 连接, 等待任务完成
        await tcp_connections.close()
        await tcp_server.wait_closed()

app = FastAPI(
    title="Cloudflare Warp API Bridge",
    # 文档展示联系方式
    contact={
        "name": "Yupeng",
        "email": "yangyupeng290@gmail.com"
    },
    # swagger ui
    swagger_ui_parameters={
        # 是否显示搜索框
        "filter": True,
        # 是否显示 Schemas
        # -1: 不显示, 0: 显示不展开, 1: 展开, 2: 全部展开
        "defaultModelsExpandDepth": -1,
        # 是否显示请求耗时
        "displayRequestDuration": True,
    },

    # 生命周期管理器
    lifespan=lifespan,
    debug=DEV_MODE,
)

@app.middleware("http")
async def check_request(
    req: Request,
    call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    # 获取客户端
    req_client = req.client
    if req_client is None:
        return Response(
            content="Client information not available.",
            status_code=status.HTTP_400_BAD_REQUEST
        )
    # 获取客户端 IP 地址
    client_ip = req_client.host
    try:
        resolved_ip = ip_address(client_ip)
    except ValueError:
        return Response(
            content="Invalid IP address.",
            status_code=status.HTTP_400_BAD_REQUEST
        )

    # 检查是否在阻止列表中
    if BLOCK_IPS is not None and resolved_ip in BLOCK_IPS:
        return JSONResponse(
            content={
                "detail": "Access denied."
            },
            status_code=status.HTTP_403_FORBIDDEN
        )
    return await call_next(req)

if __name__ == "__main__":
    from typing import Final
    import signal
    import uvicorn

    SERVER_PORT = settings.service_port             # 服务监听的 HTTP 端口
    TRUSTED_PROXY_IPS = settings.trusted_proxy_ips  # 允许的代理 IP 地址列表
    # 平台退出信号
    # - SIGHUP: 1, 终端关闭 (Linux)
    # - SIGBREAK: 21, 控制台 Ctrl+Break (Windows)
    # - SIGINT: 2, Ctrl+C
    # - SIGTERM: 15, kill <pid>
    SHUTDOWN_SIGNALS = settings.shutdown_signals # Linux: (SIGHUP, )  Windows: (SIGBREAK, )

    file_stem = Path(__file__).stem
    serve_name: Final[str] = f"{file_stem}:app"
    # signal.signal 类似 JS 的 addEventListener("", () => {}) 监听事件
    signal.signal(
        signal.SIGTERM,  # 15, kill <pid>
        # 收到 SIGTERM 信号时, 抛出异常
        # 让 main 函数直接执行 finally 分支
        lambda _s, _f: (_ for _ in ()).throw(SystemExit(1))
    )
    for s_signal in SHUTDOWN_SIGNALS:
        # 当收到平台退出信号时, 手动触发 SIGTERM 信号 (kill <pid>), 进入 finally 分支
        signal.signal(
            s_signal,
            # signal.raise_signal 类似 JS 的 target.dispatchEvent(event) 手动触发事件
            # 收到退出信号时, 手动触发 SIGTERM 信号
            # 因为 uvicorn 只监听 SIGTERM 和 SIGINT 信号, 其他信号不会触发 uvicorn 的 shutdown
            lambda _s, _f: signal.raise_signal(signal.SIGTERM)
        )

    try:
        # 启动设置脚本
        awaitable = run_command("start")
        asyncio.run(awaitable)

        uvicorn.run(
            serve_name,
            host="127.0.0.1",
            port = SERVER_PORT,
            # 是否信任代理头部信息
            proxy_headers=True,
            # 允许的代理 IP 地址列表
            forwarded_allow_ips=TRUSTED_PROXY_IPS,
            # 日常运行时关闭热重载
            # 避免保存文件导致桥接连接被中断
            # 避免等待 TCP 客户端超时不关闭 Python 进程
            reload=DEV_MODE,
        )
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
        awaitable = run_command(
            "close",
            timeout=CLOSE_COMMAND_TIMEOUT
        )
        asyncio.run(awaitable)
