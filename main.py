from __future__ import annotations
from typing import (
    Callable,
    Awaitable,
    Optional,
    TYPE_CHECKING,
)
if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
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

TCP_HOST = settings.tcp_host
TCP_PORT = settings.tcp_port
BLOCK_IPS = settings.block_ips
HTTPS_PORT = settings.https_port
DEV_MODE = settings.dev_mode
# 网络适配配置
WARP_TUNNEL_ENDPOINT = settings.warp_tunnel_endpoint

# 启停流程配置
WARP_SVC_WAIT_TIMEOUT = settings.warp_svc_wait_timeout
SUDO_REFRESH_INTERVAL = settings.sudo_refresh_interval
WARP_CONNECT_WAIT_TIMEOUT = settings.warp_connect_wait_timeout
CLOSE_COMMAND_TIMEOUT = settings.close_command_timeout

PROJECT_ROOT = Path(__file__).resolve().parent

async def run_command(
    script_name: str,
    timeout: Optional[float] = None,
):
    COMMAND_PATH = PROJECT_ROOT / "command"
    if sys.platform == "linux":
        command_file = COMMAND_PATH / "linux" / f"{script_name}.sh"
        command = [
            "bash",
            str(command_file),
        ]
    elif sys.platform == "win32":
        raise NotImplementedError(
            "Windows platform is not supported yet."
        )
    else:
        raise NotImplementedError(
            f"Unsupported platform: {sys.platform}"
        )

    # 启动脚本, 所有可调参数统一来自 Settings, 按位置传入
    # 顺序需与 command/<平台>/ 下脚本的参数接收顺序保持一致
    process = await asyncio.create_subprocess_exec(
        *command,
        TCP_HOST,                       # $1
        str(TCP_PORT),                  # $2
        str(HTTPS_PORT),                # $3
        WARP_TUNNEL_ENDPOINT or "",     # $4
        str(WARP_SVC_WAIT_TIMEOUT),     # $5
        str(SUDO_REFRESH_INTERVAL),     # $6
    )

    try:
        return_code = await asyncio.wait_for(process.wait(), timeout)
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

async def connect_warp():
    process = await asyncio.create_subprocess_exec(
        "warp-cli",
        "connect",
    )

    if await process.wait() != 0:
        raise RuntimeError("Failed to connect WARP.")

async def wait_warp_connected(
    timeout: float = WARP_CONNECT_WAIT_TIMEOUT,
) -> None:
    # warp-cli connect 只是发出连接请求, 立即返回
    # 这里轮询 WARP 状态, 真正连上后再记录日志
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while loop.time() < deadline:
        process = await asyncio.create_subprocess_exec(
            "warp-cli",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        if b"Status update: Connected" in stdout:
            logger.info("WARP connected.")
            return
        await asyncio.sleep(1)

    logger.warning(
        f"WARP is not connected after {timeout:.0f}s, "
        f"check `warp-cli status`."
    )

@asynccontextmanager
async def lifespan(
    app: FastAPI,
) -> "AsyncGenerator[None, None]":
    tcp_connections = TCPConnections()
    tcp_server = await asyncio.start_server(
        tcp_connections.accept,
        host=TCP_HOST,
        port=TCP_PORT,
    )
    # 打印日志
    logger.info(f"WARP TCP bridge starting to listen on {TCP_HOST}:{TCP_PORT}")

    wait_task: Optional["asyncio.Task[None]"] = None
    try:
        await connect_warp()
        # 后台等待 WARP 连上, 不阻塞服务启动
        wait_task = asyncio.create_task(wait_warp_connected())
        yield
    finally:
        if wait_task is not None and not wait_task.done():
            wait_task.cancel()
            with suppress(asyncio.CancelledError):
                await wait_task

        # 先停止接入, 再等待已有连接完成取消和清理
        tcp_server.close()
        logger.info("TCP bridge shutting down...")

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

    SERVER_PORT = settings.service_port
    TRUSTED_PROXY_IPS = settings.trusted_proxy_ips
    # 平台相关的退出信号, 由 Settings 按平台确定
    SHUTDOWN_SIGNALS = settings.shutdown_signals

    file_stem = Path(__file__).stem
    serve_name: Final[str] = f"{file_stem}:app"

    # =================== 退出信号处理 ===================
    # 目标: 无论以何种方式退出, 都执行 finally 中的 close 脚本, 恢复 WARP 初始状态
    # - Ctrl+C (SIGINT): Python 默认抛出 KeyboardInterrupt, 本身就会进入 finally
    # - kill (SIGTERM): uvicorn 优雅关闭后会重新触发 SIGTERM, 默认处理会直接杀死进程,
    #   跳过 finally; 因此改为抛出 SystemExit
    # - 平台退出信号 (Linux 关闭终端 SIGHUP / Windows Ctrl+Break SIGBREAK):
    #   默认直接退出, 跳过 finally; 因此转为 SIGTERM, 走同样的优雅关闭流程
    # 信号处理函数会收到 (信号编号, 调用栈) 两个参数, 这里都不需要, 用 *_ 忽略
    def exit_on_sigterm(*_: object) -> None:
        raise SystemExit(1)

    def forward_to_sigterm(*_: object) -> None:
        signal.raise_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, exit_on_sigterm)
    for shutdown_signal in SHUTDOWN_SIGNALS:
        signal.signal(shutdown_signal, forward_to_sigterm)

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
        # 恢复过程中忽略退出信号, 避免再次 Ctrl+C 或终端关闭打断恢复
        # 忽略状态会被 close 脚本及其子进程继承
        for exit_signal in (signal.SIGINT, signal.SIGTERM, *SHUTDOWN_SIGNALS):
            signal.signal(exit_signal, signal.SIG_IGN)

        awaitable = run_command("close", timeout=CLOSE_COMMAND_TIMEOUT)
        asyncio.run(awaitable)
