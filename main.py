from __future__ import annotations
from typing import (
    Callable,
    Awaitable,
    TYPE_CHECKING,
)
if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
    )

from contextlib import (
    asynccontextmanager
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

PROJECT_ROOT = Path(__file__).resolve().parent

async def run_command(
    script_name: str
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

    # 启动脚本
    process = await asyncio.create_subprocess_exec(
        *command,
        TCP_HOST,
        str(TCP_PORT),
        str(HTTPS_PORT),
        WARP_TUNNEL_ENDPOINT or "",
    )

    return_code = await process.wait()
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

    try:
        await connect_warp()
        yield
    finally:
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
    debug=True,
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
    import uvicorn

    SERVER_PORT = settings.service_port
    TRUSTED_PROXY_IPS = settings.trusted_proxy_ips

    file_stem = Path(__file__).stem
    serve_name: Final[str] = f"{file_stem}:app"

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
        awaitable = run_command("close")
        asyncio.run(awaitable)
