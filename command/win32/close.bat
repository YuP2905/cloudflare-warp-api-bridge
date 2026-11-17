@echo off
setlocal

rem 网络适配配置
set "WARP_TUNNEL_ENDPOINT=%~4"

rem 恢复时继续执行每一项清理, 不提前退出
rem 0 表示全部成功; 任意一步失败后保留为 1, 最后交给 Python 判断
set "EXIT_CODE=0"

rem 断开当前 WARP 连接
rem 实测: Windows 上只在界面中断开, 网络可能仍无法使用, 执行 disconnect 后才恢复
warp-cli disconnect || set "EXIT_CODE=1"

rem =================== 网络适配恢复 ===================
rem 撤销本项目设置的自定义 MASQUE 入口
if not "%WARP_TUNNEL_ENDPOINT%"=="" (
    warp-cli tunnel endpoint reset || set "EXIT_CODE=1"
)

rem =================== TCP Bridge 恢复 ===================
rem 恢复 WARP 默认控制 API 地址
warp-cli api endpoint reset || set "EXIT_CODE=1"

rem run_command 会将非零退出码报告为异常
exit /b %EXIT_CODE%
