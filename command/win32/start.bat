@echo off
setlocal

rem 接收配置
set "TCP_HOST=%~1"

rem 网络适配配置
set "WARP_TUNNEL_ENDPOINT=%~4"

rem 启停流程配置
rem 等待 WARP 后台服务响应命令的最长时间 (秒)
set "WARP_SVC_WAIT_TIMEOUT=%~5"

rem WARP 后台服务 (warp-svc.exe) 的 Windows 服务名称
set "WARP_SERVICE_NAME=%~6"

rem =================== WARP 后台服务 ===================
rem 启动 WARP 后台服务, 已经在运行则跳过 (启动服务需要管理员权限)
sc query "%WARP_SERVICE_NAME%" | findstr /c:"RUNNING" >nul
if errorlevel 1 (
    net start "%WARP_SERVICE_NAME%" >nul || (
        echo Failed to start the %WARP_SERVICE_NAME% service. 1>&2
        exit /b 1
    )
)

rem 等待 WARP 后台服务可以响应命令
set "WAITED=0"
:wait_warp_svc
warp-cli status >nul 2>&1 && goto warp_svc_ready
rem 超时后再执行一次 warp-cli status, 不隐藏输出, 显示真实的错误 (例如 拒绝访问)
if %WAITED% geq %WARP_SVC_WAIT_TIMEOUT% (
    echo warp-svc did not respond within %WARP_SVC_WAIT_TIMEOUT%s. 1>&2
    warp-cli status 1>&2
    exit /b 1
)
rem 等待 1 秒: ping 本机两次, 间隔约 1 秒 (不依赖控制台输入, 比 timeout 命令稳定)
ping -n 2 127.0.0.1 >nul
set /a WAITED+=1
goto wait_warp_svc
:warp_svc_ready

rem 断开当前 WARP 连接 (连接状态下修改设置, WARP 也会自动断开)
warp-cli disconnect || exit /b 1

rem =================== 网络适配 ===================
rem 使用自定义 MASQUE UDP 入口
if not "%WARP_TUNNEL_ENDPOINT%"=="" (
    warp-cli tunnel endpoint set %WARP_TUNNEL_ENDPOINT% || exit /b 1
)

rem 修改 WARP 控制 API 地址
rem Windows 上 TCP Bridge 直接监听 443 端口, 不需要端口转发
warp-cli api endpoint set %TCP_HOST% || exit /b 1
