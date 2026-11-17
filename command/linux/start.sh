#!/usr/bin/env bash
set -e

# 接收配置
TCP_HOST="$1"
TCP_PORT="$2"
HTTPS_PORT="$3"
# 网络适配配置
WARP_TUNNEL_ENDPOINT="$4"

# 启停流程配置 (统一在 config.py 的 Settings 中设置)
# 等待 warp-svc 响应命令的最长时间 (秒)
WARP_SVC_WAIT_TIMEOUT="$5"
# 运行期间刷新 sudo 授权的间隔 (秒)
SUDO_REFRESH_INTERVAL="$6"

# =================== sudo 授权 ===================
# 启动时统一输入一次 sudo 密码
sudo -v

# 项目运行期间, 定期刷新 sudo 授权
# 保证退出时 close 脚本 (sudo -n) 不会因授权过期而失败
# $PPID 为 Python 主进程, 主进程退出后循环自动结束
BRIDGE_PID="$PPID"
(
    while kill -0 "$BRIDGE_PID" 2>/dev/null; do
        sleep "$SUDO_REFRESH_INTERVAL"
        sudo -n -v 2>/dev/null || true
    done
) </dev/null >/dev/null 2>&1 &

# 启动 WARP 后台服务
sudo systemctl start warp-svc

# 等待 WARP 后台服务可以响应命令
WAITED=0
until warp-cli status >/dev/null 2>&1; do
    if ! systemctl is-active --quiet warp-svc; then
        echo "warp-svc is not active." >&2
        exit 1
    fi
    if [ "$WAITED" -ge "$WARP_SVC_WAIT_TIMEOUT" ]; then
        echo "warp-svc did not respond within ${WARP_SVC_WAIT_TIMEOUT}s." >&2
        exit 1
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

# 断开当前 WARP 连接
warp-cli disconnect

# =================== 网络适配 ===================
# 使用自定义 MASQUE UDP 入口
if [ -n "$WARP_TUNNEL_ENDPOINT" ]; then
    sudo warp-cli tunnel endpoint set "$WARP_TUNNEL_ENDPOINT"
fi

# 修改 WARP 控制 API 地址
sudo warp-cli api endpoint set "$TCP_HOST"

# 将控制 API 的 TCP 443 连接转发到本地 TCP 服务
if ! sudo iptables -t nat -C OUTPUT \
    -p tcp -d "$TCP_HOST" --dport "$HTTPS_PORT" \
    -j REDIRECT --to-ports "$TCP_PORT" 2>/dev/null; then
    sudo iptables -t nat -I OUTPUT \
        -p tcp -d "$TCP_HOST" --dport "$HTTPS_PORT" \
        -j REDIRECT --to-ports "$TCP_PORT"
fi
