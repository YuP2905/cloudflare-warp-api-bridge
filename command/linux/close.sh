#!/usr/bin/env bash

# 接收配置
TCP_HOST="$1"
TCP_PORT="$2"
HTTPS_PORT="$3"
# 网络适配配置
WARP_TUNNEL_ENDPOINT="$4"

# 恢复时继续执行每一项清理
# 0 表示全部成功; 任意一步失败后保留为 1, 最后交给 Python 判断
EXIT_CODE=0

# 所有 sudo 使用 -n: 授权失效时立即失败, 不在退出阶段卡住等待输入密码
# 授权由 start 脚本在运行期间保持刷新
if ! sudo -n true 2>/dev/null; then
    echo "sudo authorization is not available, privileged restore steps will fail." >&2
    EXIT_CODE=1
fi

# 断开当前 WARP 连接
warp-cli disconnect || EXIT_CODE=1

# =================== 网络适配恢复 ===================
# 撤销本项目设置的自定义 MASQUE 入口
if [ -n "$WARP_TUNNEL_ENDPOINT" ]; then
    sudo -n warp-cli tunnel endpoint reset || EXIT_CODE=1
fi

# =================== TCP Bridge 恢复 ===================
# 恢复 WARP 默认控制 API 地址
sudo -n warp-cli api endpoint reset || EXIT_CODE=1

# 删除 TCP 端口转发规则
if sudo -n iptables -t nat -C OUTPUT \
    -p tcp -d "$TCP_HOST" --dport "$HTTPS_PORT" \
    -j REDIRECT --to-ports "$TCP_PORT" 2>/dev/null; then
    sudo -n iptables -t nat -D OUTPUT \
        -p tcp -d "$TCP_HOST" --dport "$HTTPS_PORT" \
        -j REDIRECT --to-ports "$TCP_PORT" || EXIT_CODE=1
fi

# 原有 run_command 会将非零退出码报告为异常
exit "$EXIT_CODE"
