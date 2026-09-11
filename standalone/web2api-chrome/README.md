# Web2API + Chrome 独立双路 API 模块

这个目录可以脱离 AI Chat Platform 网站单独使用。它通过 Chrome 控制已登录的 ChatGPT 网页，并在本机提供一个 OpenAI 兼容 API。

> 这不是 OpenAI 官方 API，也不会生成官方 API Key。模块操作的是你自己的 ChatGPT 网页账号；可用模型、限额、速度和稳定性取决于网页账号与 ChatGPT 页面变化。请遵守相关服务条款，不要用于未获授权的账号或大规模滥用。

## 你会得到什么

- 一个本机 OpenAI 兼容地址：`http://127.0.0.1:9181/v1`
- 两个专用 ChatGPT 标签页，最多同时处理两个聊天请求
- 流式与非流式聊天回答
- 网页生图会等待最多 15 分钟，适应官网生成较慢的情况
- 生成后的图片优先使用二进制下载；若不可用会自动退回原兼容通道
- 独立 Chrome 登录资料，不影响日常浏览器账号
- 意外关闭后的自动恢复
- 系统托盘后台运行，不占用任务栏
- 可选 Cloudflare Tunnel 公网入口

## 工作方式

```text
你的程序 / OpenAI SDK / curl
  │
  │  POST /v1/chat/completions
  ▼
双路网关 127.0.0.1:9181
  ├─ 请求 1 → Web2API :9182 → Chrome CDP :9325 → 专用标签页 1
  └─ 请求 2 → Web2API :9183 → Chrome CDP :9325 → 专用标签页 2
```

一次请求的实际过程：

1. 你的程序向 `9181` 发送 OpenAI 格式的请求。
2. 双路网关选择当前空闲的工作进程。
3. Web2API 通过 CDP 找到属于自己的专用标签页。
4. Web2API 在网页输入框中写入消息并发送。
5. 它持续读取网页状态，直到确认回答真正结束。
6. 回答被转换为 OpenAI 兼容 JSON 或 SSE 流并返回给你的程序。

`9182` 和 `9183` 各控制一个标签页，所以容量是两路。第三个及之后的请求需要等待空闲位置；多开普通 ChatGPT 标签页不会自动提高并发。

## 端口分别做什么

| 端口 | 用途 | 是否应直接公开 |
|---|---|---|
| `9181` | 双路 API 总入口 | 只建议通过带 HTTPS 的 Tunnel 发布 |
| `9182` | Web2API 工作进程 1 | 否 |
| `9183` | Web2API 工作进程 2 | 否 |
| `9325` | Chrome 本机调试接口 | 绝对不要公开 |

## Web2API + Chrome：从这里开始

### 1. 准备软件

在 Windows 10/11 电脑上安装：

- [Git](https://git-scm.com/download/win)
- [Python 3.11 或更新版本](https://www.python.org/downloads/windows/)
- [Google Chrome](https://www.google.com/chrome/)

安装 Python 时勾选 **Add Python to PATH**。这台电脑需要在使用 API 时保持开机和联网。

### 2. 只下载这个模块

在 PowerShell 中执行以下命令。稀疏下载只会取出这个独立目录：

```powershell
git clone --filter=blob:none --sparse https://github.com/Jiu1317/ai-chat-platform.git
cd ai-chat-platform
git sparse-checkout set standalone/web2api-chrome
cd standalone\web2api-chrome
```

如果你已经下载了完整仓库，直接进入：

```powershell
cd ai-chat-platform\standalone\web2api-chrome
```

### 3. 安装模块

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup-windows.ps1
```

脚本会在当前目录创建独立 Python 环境，并生成 `web2api-config.json`。它不会读取你的普通 Chrome 资料。

### 4. 生成自己的 API 密钥

```powershell
[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLower()
```

复制输出的随机字符串，打开 `web2api-config.json`，把 `CHANGE_ME_TO_A_LONG_RANDOM_VALUE` 替换成刚生成的字符串并保存。不要把真实密钥发给别人，也不要提交 `web2api-config.json`。

### 5. 第一次启动并登录

```powershell
.\start-dual-tab.ps1
```

第一次会打开独立 Chrome 窗口和两个专用标签页。请手动登录你自己的 ChatGPT 账号；不要把账号密码写进脚本。完成登录后，再执行一次：

```powershell
.\start-dual-tab.ps1
```

以后会复用这个独立登录资料。

### 6. 检查双路状态

```powershell
Invoke-RestMethod http://127.0.0.1:9181/health
```

正常结果应包含：

```text
status: healthy
capacity: 2
ready_backends: 2
```

如果 `ready_backends` 不是 `2`，先检查专用 Chrome 是否已登录，再查看当前目录的 `dual-worker-*.stderr.log`。

### 7. 调用 API

把下面的 `YOUR_PRIVATE_API_KEY` 换成你在第 4 步生成的密钥：

```powershell
$headers = @{
    Authorization = 'Bearer YOUR_PRIVATE_API_KEY'
    'Content-Type' = 'application/json'
}
$body = @{
    model = 'gpt-5-5'
    messages = @(
        @{ role = 'user'; content = '你好，请用一句话介绍你自己。' }
    )
    stream = $false
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
    -Uri 'http://127.0.0.1:9181/v1/chat/completions' `
    -Method Post `
    -Headers $headers `
    -Body $body
```

第三方程序中的填写方式：

- API 地址：`http://127.0.0.1:9181/v1`
- 协议：OpenAI 兼容
- API 密钥：你在 `web2api-config.json` 中设置的密钥

### 连续对话和长上下文

第一次提问时不填写 `conversation_id`。模块最多携带 31 个完整用户轮次（30 个历史轮次加当前轮次），并把最终发送到网页的总上下文限制在 150,000 个字符以内，其中系统提示词最多 24,000 个字符；超长内容会保留开头和结尾，避免把输入框塞满。历史图片不会反复上传，只有最后一条 `user` 消息里的图片会进入本次请求。每次最多 8 张参考图，单张原始图片和同一请求全部图片合计都不得超过 30 MiB；生成后下载及缓存的每个图片或文件资产也不得超过 30 MiB。

成功回答会返回官网会话编号 `conversation_id`。下一次提问把它放在请求最外层，即可继续同一个官网对话：

```powershell
$body = @{
    model = 'gpt-5-5'
    conversation_id = '把上一次响应中的 conversation_id 填在这里'
    messages = @(
        @{ role = 'user'; content = '请接着上一个问题继续说明。' }
    )
    stream = $false
} | ConvertTo-Json -Depth 10
```

填写 `conversation_id` 后，模块只发送最后一条用户消息和本轮图片；官网对话本身负责保留之前内容，所以不会把历史重复发送一遍。切换模型、项目或系统提示词时，请不带旧的 `conversation_id` 发起新对话。请求的最后一条对话消息必须是 `user`，否则会返回 400，防止误把已回答的问题再次发送。

### 8. 放到系统托盘后台运行

确认双路状态正常后执行：

```powershell
.\install-background.ps1
```

专用浏览器会隐藏到右下角系统托盘，不再占用任务栏。双击托盘图标可显示浏览器；右键可选择显示或隐藏。误关浏览器后，守护程序会检测并自动恢复。

### 9. 正常停止

```powershell
.\stop-dual-tab.ps1
```

停止脚本会创建暂停标记，守护程序不会把主动停止误判为崩溃。再次执行 `start-dual-tab.ps1` 会解除暂停。

## 可选：通过 Cloudflare Tunnel 发布

只有其他设备确实需要访问时才配置公网入口。先安装 `cloudflared`，然后创建自己的 Tunnel：

```powershell
cloudflared tunnel login
cloudflared tunnel create web2api-local
cloudflared tunnel route dns web2api-local api.example.com
Copy-Item .\cloudflared-config.example.yml .\cloudflared-config.yml
```

编辑 `cloudflared-config.yml`，替换示例 Tunnel UUID、凭据路径和 `api.example.com`，然后启动：

```powershell
.\start-cloudflare-tunnel.ps1
```

公网客户端填写 `https://api.example.com/v1`。不要直接开放 `9182`、`9183` 或 `9325`，也不要关闭 API 密钥验证。

## 文件说明

```text
web2api/                       Web2API Python 源码与许可证
dual_tab_gateway.py            双路请求分配网关
setup-windows.ps1              首次安装
start-dual-tab.ps1             启动两个工作进程和网关
stop-dual-tab.ps1              主动停止并暂停自动恢复
watch-dual-tab.ps1             意外关闭检测与自动恢复
dual-tab-tray.ps1              系统托盘显示/隐藏控制
install-background.ps1         安装托盘与登录自启动
web2api-config.example.json    不含真实密钥的配置模板
cloudflared-config.example.yml 不含真实凭据的 Tunnel 模板
start-cloudflare-tunnel.ps1    可选 Tunnel 启动脚本
```

## 安全说明

- 不要上传 `web2api-config.json`、`cloudflared-config.yml`、日志或 Chrome 登录资料。
- API 密钥至少使用 32 字节随机值；本机和公网入口不要使用空密钥。
- `9325` 只能监听本机，不能映射到路由器或公网。
- 专用 Chrome 仅用于桥接，不要作为日常浏览器使用。
- 不要把此模块提供给不可信用户共同操作同一个 ChatGPT 账号。

完整聊天网站、账户管理和客户端请返回[项目主页](../../README.md)。
