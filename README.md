# Cloudflare WARP API Bridge

[English](#english) | [中文](#中文)

## English

A local TCP bridge that sends the Cloudflare WARP client's control API traffic through the WARP tunnel, so that WARP can be turned on even on networks that replace TLS certificates (for example, a school network). It sets up WARP automatically on start and restores it on exit.

### Background

The school network inspects HTTPS traffic: a device in the middle intercepts the connection and replaces the server's certificate with one issued by the school. The school uses this to cut off access to certain websites and redirect them to its block page.

Normally, turning on Cloudflare WARP would encrypt the traffic before it goes out. But while it runs, the WARP client needs to reach Cloudflare's control API (`api.devices.cloudflare.com`, official IPs `162.159.137.105` and `162.159.138.105`, port 443). This HTTPS traffic does not go through the WARP tunnel; it leaves directly from the local network, so the school replaces its certificate too. The WARP client fails to verify the certificate, the control API requests fail, and WARP cannot be turned on.

This project sends the control API traffic through the WARP tunnel as well. The school network then only sees encrypted tunnel traffic and can no longer replace the certificate, so WARP works normally.

### How it works

```
WARP client (warp-svc)
   │  control API address changed to 127.0.0.1; WARP always connects to port 443
   ▼
127.0.0.1:443
   │  Linux: iptables redirects 443 to 8843
   │  Windows: listens on 443 directly
   ▼
Local TCP bridge
   │  upstream connection bound to the WARP interface CloudflareWARP
   │  Linux: SO_BINDTODEVICE / Windows: IP_UNICAST_IF
   ▼
WARP tunnel ──▶ Cloudflare control API (162.159.137.105 / 162.159.138.105 :443)
```

- **Forwards only, never decrypts**: the bridge passes TCP data through unchanged. TLS still runs end to end between the WARP client and Cloudflare, with Cloudflare's real certificate.
- **On start**: start the WARP service → disconnect WARP → set the tunnel endpoint (optional) and the control API address → add the iptables redirect rule (Linux only) → start the bridge → connect WARP.
- **On exit**: stop the bridge → disconnect WARP → restore the tunnel endpoint and the control API address → remove the iptables rule (Linux only).
- **FastAPI service**: the program also runs a FastAPI service (`127.0.0.1:8765`), which currently only hosts the bridge's lifecycle.

### Supported platforms

| Platform | Status |
|---|---|
| Linux (systemd + iptables) | Supported; tested on Ubuntu and in a Proxmox LXC container |
| Windows 11 22H2 or later | Supported |
| macOS | Not supported |

### Requirements

- **Python**: 3.12 or later; [uv](https://docs.astral.sh/uv/) is recommended for managing dependencies.
- **WARP client**: Cloudflare WARP installed, `warp-cli` working, and the device already registered.
- **Linux**: `sudo` access (you enter the password once at start), systemd and iptables.
- **Windows**: run it from an administrator terminal.

### Installation

```bash
git clone https://github.com/YuP2905/cloudflare-warp-api-bridge.git
cd cloudflare-warp-api-bridge
uv sync
```

### Configuration

Copy `.env.example` to `.env` and edit it as needed. Setting names are case-sensitive and all lowercase.

```env
dev_mode = False
warp_tunnel_endpoint = 162.159.197.2:4500
```

| Setting | Default | Description |
|---|---|---|
| `dev_mode` | `True` | Development mode; uvicorn reloads on file changes. Set it to `False` for daily use; **it must be `False` on Windows** |
| `warp_tunnel_endpoint` | none | Custom MASQUE tunnel endpoint, as `IPv4:UDP port`. If unset, WARP's default endpoint is used |
| `tcp_host` | `127.0.0.1` | Address the bridge listens on; also written to WARP as the control API address |
| `tcp_port` | Linux `8843`, Windows `443` | Port the bridge listens on. It must be 443 on Windows |
| `service_port` | `8765` | Port of the FastAPI service |
| `block_ips` | none | Client IPs to block, as JSON, e.g. `["192.168.1.10"]` |
| `trusted_proxy_ips` | none | Trusted proxy IPs, used to read the real client IP |
| `chunk_size` | `65536` | Maximum number of bytes read per forward |
| `connect_timeout` | `4.0` | Timeout for connecting to the control API (seconds) |
| `keepalive_idle` / `keepalive_interval` / `keepalive_count` | `30` / `10` / `3` | TCP keepalive for upstream connections, to notice a dropped tunnel quickly |
| `warp_svc_wait_timeout` | `30` | Max time the start script waits for the WARP service to be ready (seconds) |
| `warp_connect_wait_timeout` | `60` | Max time to wait for WARP to connect (seconds); only logs a warning on timeout |
| `close_command_timeout` | `60` | Max run time of the restore script on exit (seconds) |
| `sudo_refresh_interval` | `60` | Linux only: how often sudo authorization is refreshed while running (seconds) |

### Running

```bash
uv run python main.py
```

- **Linux**: you are asked for the sudo password once at start.
- **Windows**: run it from an administrator terminal.

These two log lines mean everything works:
- `WARP connected.`: WARP is connected.
- `WARP TCP bridge connected: client=..., upstream=...`: WARP's control API requests are going through the bridge.

### Stopping

Press Ctrl+C **once**. The program stops the bridge first, then restores the WARP settings automatically. Do not press Ctrl+C again while it restores.

| Platform | Exit methods that restore automatically |
|---|---|
| Linux | Ctrl+C, `kill <pid>`, closing the terminal |
| Windows | Ctrl+C, Ctrl+Break |

- **Do not close the window on Windows**: after the window closes, Windows ends the process within about 5 seconds, which is not enough time to restore WARP.
- **"Terminate batch job (Y/N)?" on Windows**: if this prompt appears while restoring, answer **N**.
- **Cases that cannot be restored automatically**: `kill -9`, power loss, forced shutdown.

### Manual recovery after an abnormal exit

If the program did not finish restoring (for example, the window was closed or the process was killed), WARP stays in the modified state and the network may not work. Run these commands by hand.

**Windows:**

```bash
warp-cli disconnect
warp-cli tunnel endpoint reset
warp-cli api endpoint reset
```

**Linux** (also remove the iptables redirect rule; `8843` is the value of `tcp_port`):

```bash
warp-cli disconnect
sudo warp-cli tunnel endpoint reset
sudo warp-cli api endpoint reset
sudo iptables -t nat -D OUTPUT -p tcp -d 127.0.0.1 --dport 443 -j REDIRECT --to-ports 8843
```

### Project structure

```
├── main.py              # entry point: start/stop scripts, FastAPI app, lifecycle, exit signals
├── config.py            # Settings, split by platform into LinuxSettings / WindowsSettings
├── services/
│   ├── tcp.py           # client connections: accept, track, close
│   ├── bridge.py        # two-way forwarding for one connection
│   └── network.py       # bind upstream sockets to the WARP interface, enable keepalive
└── command/
    ├── linux/           # start.sh / close.sh
    └── win32/           # start.bat / close.bat
```

## 中文

一个本地 TCP 桥接服务，把 Cloudflare WARP 客户端访问控制 API 的流量改走 WARP 隧道。这样在会替换 TLS 证书的网络（比如学校网络）里，WARP 也能正常开启。程序启动和退出时，会自动完成 WARP 的设置和恢复。

### 背景

学校网络会对 HTTPS 流量做 TLS 检查：网络中间的设备拦下连接，把服务器证书换成学校自己签发的证书。学校靠这种方式直接切断对某些网站的访问，并把请求重定向到学校的屏蔽页面。

正常情况下，开启 Cloudflare WARP 就能先加密流量再访问。但 WARP 客户端运行时需要访问 Cloudflare 的控制 API（`api.devices.cloudflare.com`，官方 IP 为 `162.159.137.105` 和 `162.159.138.105`，端口 443）。这部分 HTTPS 流量不经过 WARP 隧道，而是直接从本机网络发出，所以同样会被学校替换证书。WARP 客户端校验证书失败，控制 API 请求就无法完成，WARP 也就开不起来。

本项目让控制 API 的流量也走 WARP 隧道。这样学校网络只能看到加密后的隧道流量，没法再替换证书，WARP 就能正常工作。

### 工作原理

```
WARP 客户端 (warp-svc)
   │  控制 API 地址被改为 127.0.0.1, WARP 固定连接 443 端口
   ▼
127.0.0.1:443
   │  Linux: iptables 把 443 转发到 8843
   │  Windows: 直接监听 443
   ▼
本地 TCP 桥接服务
   │  上游连接绑定到 WARP 网卡 CloudflareWARP
   │  Linux: SO_BINDTODEVICE / Windows: IP_UNICAST_IF
   ▼
WARP 隧道 ──▶ Cloudflare 控制 API (162.159.137.105 / 162.159.138.105 :443)
```

- **只转发，不解密**：桥接服务把 TCP 数据原样转发。TLS 仍然由 WARP 客户端和 Cloudflare 两端直接完成，证书是真正的 Cloudflare 证书。
- **启动时**：启动 WARP 后台服务 → 断开 WARP → 设置隧道入口（可选）和控制 API 地址 → 添加 iptables 转发规则（仅 Linux） → 启动桥接服务 → 连接 WARP。
- **退出时**：关闭桥接服务 → 断开 WARP → 恢复隧道入口和控制 API 地址 → 删除 iptables 规则（仅 Linux）。
- **FastAPI 服务**：程序还会运行一个 FastAPI 服务（`127.0.0.1:8765`），目前只用来托管桥接服务的生命周期。

### 支持平台

| 平台 | 状态 |
|---|---|
| Linux（systemd + iptables） | 支持，已在 Ubuntu 和 Proxmox LXC 容器中测试 |
| Windows 11 22H2 及以上 | 支持 |
| macOS | 不支持 |

### 环境要求

- **Python**：3.12 及以上，推荐用 [uv](https://docs.astral.sh/uv/) 管理依赖。
- **WARP 客户端**：已安装 Cloudflare WARP 客户端，`warp-cli` 可以正常使用，并且已经完成注册。
- **Linux**：需要 `sudo` 权限（启动时输入一次密码），以及 systemd 和 iptables。
- **Windows**：在管理员终端中运行。

### 安装

```bash
git clone https://github.com/YuP2905/cloudflare-warp-api-bridge.git
cd cloudflare-warp-api-bridge
uv sync
```

### 配置

把 `.env.example` 复制为 `.env`，再按需修改。配置项名称区分大小写，全部用小写。

```env
dev_mode = False
warp_tunnel_endpoint = 162.159.197.2:4500
```

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `dev_mode` | `True` | 开发模式，开启后 uvicorn 会热重载。日常使用请设为 `False`，**Windows 上必须设为 `False`** |
| `warp_tunnel_endpoint` | 无 | 自定义 MASQUE 隧道入口，格式为 `IPv4:UDP端口`。不设置则使用 WARP 默认入口 |
| `tcp_host` | `127.0.0.1` | 桥接服务的监听地址，同时也会写入 WARP 作为控制 API 地址 |
| `tcp_port` | Linux `8843`，Windows `443` | 桥接服务的监听端口。Windows 上只能是 443 |
| `service_port` | `8765` | FastAPI 服务的端口 |
| `block_ips` | 无 | 禁止访问的客户端 IP 列表，JSON 格式，例如 `["192.168.1.10"]` |
| `trusted_proxy_ips` | 无 | 信任的代理 IP 列表，用来读取真实的客户端 IP |
| `chunk_size` | `65536` | 转发时单次读取的最大字节数 |
| `connect_timeout` | `4.0` | 连接控制 API 的超时时间（秒） |
| `keepalive_idle` / `keepalive_interval` / `keepalive_count` | `30` / `10` / `3` | 上游连接的 TCP keepalive 参数，用来及时发现隧道断开 |
| `warp_svc_wait_timeout` | `30` | 启动脚本等待 WARP 后台服务就绪的最长时间（秒） |
| `warp_connect_wait_timeout` | `60` | 等待 WARP 连上的最长时间（秒），超时只记录警告 |
| `close_command_timeout` | `60` | 退出时恢复脚本的最长执行时间（秒） |
| `sudo_refresh_interval` | `60` | 仅 Linux：运行期间刷新 sudo 授权的间隔（秒） |

### 运行

```bash
uv run python main.py
```

- **Linux**：启动时会提示输入一次 sudo 密码。
- **Windows**：在管理员终端中运行。

日志里出现下面两条，说明一切正常：
- `WARP connected.`：WARP 已经连上。
- `WARP TCP bridge connected: client=..., upstream=...`：WARP 的控制 API 请求正在经过桥接服务。

### 停止

按**一次** Ctrl+C。程序会先关闭桥接服务，再自动恢复 WARP 设置。恢复过程中不要再按 Ctrl+C。

| 平台 | 能自动恢复的退出方式 |
|---|---|
| Linux | Ctrl+C、`kill <pid>`、关闭终端 |
| Windows | Ctrl+C、Ctrl+Break |

- **Windows 上不要直接关闭窗口**：窗口关闭后，Windows 只等大约 5 秒就会结束进程，来不及恢复 WARP。
- **Windows 上出现"终止批处理操作吗(Y/N)?"**：恢复过程中如果看到这个提示，请选 **N**。
- **无法自动恢复的情况**：`kill -9`、断电、强制关机。

### 异常退出后手动恢复

如果程序没有走完恢复流程（比如直接关了窗口，或者进程被强制结束），WARP 会停留在修改过的状态，网络可能无法使用。这时手动执行下面的命令。

**Windows：**

```bash
warp-cli disconnect
warp-cli tunnel endpoint reset
warp-cli api endpoint reset
```

**Linux**（还需要删除 iptables 转发规则，其中 `8843` 是 `tcp_port` 的值）：

```bash
warp-cli disconnect
sudo warp-cli tunnel endpoint reset
sudo warp-cli api endpoint reset
sudo iptables -t nat -D OUTPUT -p tcp -d 127.0.0.1 --dport 443 -j REDIRECT --to-ports 8843
```

### 项目结构

```
├── main.py              # 程序入口: 启停脚本、FastAPI 应用、生命周期、退出信号处理
├── config.py            # 配置 Settings, 按平台拆分为 LinuxSettings / WindowsSettings
├── services/
│   ├── tcp.py           # 管理客户端连接: 接收、登记、关闭
│   ├── bridge.py        # 单个连接的双向转发
│   └── network.py       # 上游 Socket 绑定 WARP 网卡、开启 keepalive
└── command/
    ├── linux/           # start.sh / close.sh
    └── win32/           # start.bat / close.bat
```
