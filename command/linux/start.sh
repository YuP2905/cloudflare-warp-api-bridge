#!/usr/bin/env bash
set -e

# 接收配置
TCP_HOST="$1"
TCP_PORT="$2"
HTTPS_PORT="$3"

# 网络适配配置
WARP_TUNNEL_ENDPOINT="$4"

# 启动 WARP 后台服务
sudo systemctl start warp-svc

# 等待 WARP 后台服务可以响应命令
until warp-cli status >/dev/null 2>&1; do
    if ! systemctl is-active --quiet warp-svc; then
        exit 1
    fi
    sleep 1
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
