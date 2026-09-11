# DeepSeek Web2API + Chrome（自用实验版）

这个目录可以脱离主网站单独使用。它控制两个**独立登录的专用 Chrome**，把你自己的 DeepSeek 网页会话转换成本机 OpenAI 兼容 API。

> 本模块仅用于个人账号的自用实验，并非 DeepSeek 官方 API。网页自动化可能因服务条款、账号风控、验证码、限流或页面改版随时失效。请先阅读并遵守 DeepSeek 当前服务条款；遇到验证码、风控或限流时，本模块只会停下并提示你手动处理，不会尝试绕过。

## 第一版实验支持什么

- 普通文本对话：`deepseek-chat`
- 流式和非流式返回
- 思考模式：`deepseek-reasoner`
- 两个完全分开的 DeepSeek 专用浏览器通道
- 本机 OpenAI 兼容入口：`http://127.0.0.1:9191/v1`
- 健康检查、排队和失败提示；可选托盘与守护程序可隐藏窗口并自动恢复意外退出
- 与现有 ChatGPT Web2API 的端口、Chrome 登录资料和进程完全隔离

第一版**不承诺附件上传、图片理解或生图**。即使 DeepSeek 网页以后显示这些功能，也不代表本模块已经支持。请先把它当作“文本、流式回答、思考模式”的实验工具。

## 它是怎样工作的

```text
你的程序 / OpenAI SDK / curl
  │
  │ POST /v1/chat/completions
  ▼
双路网关 127.0.0.1:9191
  ├─ 工作进程 1 :9192 → Chrome CDP :9332 → 专用 DeepSeek 窗口 1
  └─ 工作进程 2 :9193 → Chrome CDP :9333 → 专用 DeepSeek 窗口 2
```

每个工作进程只控制自己的 Chrome 和登录目录。两个请求可以分别进入两个通道；更多请求会在网关等待空闲通道。一个账号能否同时处理两个网页会话，仍取决于 DeepSeek 的实际限制。

浏览器页面只负责确认登录和页面状态、选择模式、写入提示词、单次点击发送以及必要时停止生成。发送后，工作进程优先读取**该专用 Chrome 自己收到的 DeepSeek 回答数据流**，再转换为 OpenAI 兼容的流式结果；它不会读取 Cookie、请求头、账号资料或其他页面。只有浏览器数据流不可用时，才会保守地改从当前回答区域读取已经出现的文字，而且绝不会为此再次点击发送。

因此，专用 Chrome 隐藏后即使网页暂时不重绘，API 仍可在后台接收回答。默认启动时 Chrome 会出现在任务栏；安装本目录提供的托盘控制器后，两个专用窗口会隐藏到系统托盘，不再占用任务栏，但浏览器进程仍会继续运行，并不是真正的无界面模式。

### 端口说明

| 端口 | 用途 | 能否开放到公网 |
|---|---|---|
| `9191` | 双路 API 总入口 | 默认只供本机使用 |
| `9192` | 工作进程 1 | 不能 |
| `9193` | 工作进程 2 | 不能 |
| `9332` | 专用 Chrome 1 调试接口 | 绝对不能 |
| `9333` | 专用 Chrome 2 调试接口 | 绝对不能 |

所有端口默认只监听 `127.0.0.1`。本自用实验版不提供公网发布教程。

## Windows 从零开始

以下步骤适用于 Windows 10/11。第一次配置建议完整照做，不要与现有 ChatGPT Web2API 混用目录。

### 1. 安装需要的软件

请先安装：

- [Git for Windows](https://git-scm.com/download/win)
- [Python 3.11 或更新版本](https://www.python.org/downloads/windows/)
- [Google Chrome](https://www.google.com/chrome/)

安装 Python 时勾选 **Add Python to PATH**。运行期间电脑需要保持开机、联网，两个 DeepSeek 专用 Chrome 也必须保持登录状态。

### 2. 下载独立模块

打开 PowerShell，执行：

```powershell
git clone --filter=blob:none --sparse https://github.com/Jiu1317/ai-chat-platform.git
cd ai-chat-platform
git sparse-checkout set standalone/deepseek-web2api
cd standalone\deepseek-web2api
```

如果已经下载完整仓库，直接进入：

```powershell
cd ai-chat-platform\standalone\deepseek-web2api
```

后续命令都要在这个目录里执行。

### 3. 完成首次安装

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup-windows.ps1
```

安装脚本会创建这个模块自己的 Python 环境，并由 `config.example.json` 生成本机专用的 `config.json`。它不会复用日常 Chrome，也不会复用 `standalone/web2api-chrome` 的 ChatGPT 登录资料；如果 `config.json` 已存在，也不会覆盖。

### 4. 填写本地配置

第 3 步已经生成 `config.json`。如果文件意外缺失，才需要执行：

```powershell
Copy-Item .\config.example.json .\config.json
```

生成一个至少 32 字节的随机 API 密钥：

```powershell
[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLower()
```

用记事本打开 `config.json`，把 `server.api_keys` 中的
`REPLACE_WITH_A_LONG_RANDOM_API_KEY` 替换成刚生成的随机字符串。
`chrome.page_url` 已经填写公开的 DeepSeek 官方聊天页地址，不需要修改。

不要填写自己的聊天网站域名，也不要把账号、密码或 Cookie 写进配置。模板里的 `9192` 和 `9332` 是第一个工作进程的基础值；启动脚本会为第二个工作进程自动使用独立端口和 profile。正常使用时端口保持默认即可：

- API 网关：`9191`
- 工作进程：`9192`、`9193`
- Chrome 调试端口：`9332`、`9333`

`config.example.json` 可以上传；含有真实值的 `config.json` 不可以上传。

### 5. 第一次启动并手动登录

```powershell
.\start-deepseek-dual-tab.ps1
```

第一次会出现两个专用 Chrome。请分别在窗口中：

1. 打开 DeepSeek 网页。
2. 手动登录你自己的账号。
3. 如果出现验证码、登录确认或风控提示，由你本人手动完成。
4. 确认两个窗口都能正常发送一条普通消息。

登录完成后可以先把两个专用 Chrome 窗口最小化，API 仍会在后台工作；不要直接关闭窗口。完成下面的托盘安装后，两个窗口会从任务栏隐藏到右下角系统托盘。
需要重新登录、处理验证码或风控提示时，再恢复对应窗口手动处理。

不要把账号、密码、Cookie 写入脚本或 `config.json`。登录状态只应保存在本机专用 profile 目录中。

工作进程会实时识别登录状态，完成登录后直接进行下一步健康检查，不需要再次运行启动脚本。

### 6. 检查双路状态

```powershell
Invoke-RestMethod http://127.0.0.1:9191/health
```

应看到网关正在运行，并且两个工作进程均就绪。若只有一个通道就绪，API 仍可能处理一个请求，但不具备双路能力；若两个通道均不可用，健康检查会返回 `503`。

### 7. 测试普通文本回答

把 `YOUR_PRIVATE_API_KEY` 替换成 `config.json` 中的真实密钥：

```powershell
$headers = @{
    Authorization = 'Bearer YOUR_PRIVATE_API_KEY'
    'Content-Type' = 'application/json'
}
$body = @{
    model = 'deepseek-chat'
    messages = @(
        @{ role = 'user'; content = '你好，请只回答：连接成功' }
    )
    stream = $false
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
    -Uri 'http://127.0.0.1:9191/v1/chat/completions' `
    -Method Post `
    -Headers $headers `
    -Body $body
```

第三方 OpenAI 兼容客户端这样填写：

- API 地址：`http://127.0.0.1:9191/v1`
- API 密钥：`config.json` 中的本地密钥
- 普通模型：`deepseek-chat`
- 思考模型：`deepseek-reasoner`

### 上下文、大小与兼容字段

本模块每次都会打开一个空白 DeepSeek 对话，并把本次请求的 `messages` 按 `system`、`user`、`assistant` 顺序整理成文本后发送。因此，需要连续对话时，由调用方在下一次请求中继续带上需要的文字历史；本实验版不接受 `conversation_id` 代替历史。

网关和工作进程的单次 JSON 请求上限默认都是 **30 MiB**。这是传输上限，不代表 DeepSeek 网页一定能接受接近 30 MiB 的提示词；网页和模型自身可能更早拒绝或截断。第一版只传文字，图片与文档附件不会被悄悄忽略，而会明确返回不支持。

常见 OpenAI 客户端自动附带的 `tools: []` 和 `tool_choice: "none"` 可以正常使用；实际函数工具调用仍不支持。双路同时最多处理两个请求，额外请求进入最多 32 个位置的等待队列，默认等待空闲通道不超过 60 秒。

### 8. 测试流式回答

PowerShell 自带命令不方便直观看 SSE 分块，可使用 Windows 自带的 `curl.exe`：

```powershell
curl.exe -N http://127.0.0.1:9191/v1/chat/completions `
  -H "Authorization: Bearer YOUR_PRIVATE_API_KEY" `
  -H "Content-Type: application/json" `
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"从1数到5"}],"stream":true}'
```

正常时会连续出现以 `data:` 开头的内容，最后以 `data: [DONE]` 结束。

### 9. 测试思考模式

把模型改成 `deepseek-reasoner`：

```powershell
$body = @{
    model = 'deepseek-reasoner'
    messages = @(
        @{ role = 'user'; content = '请一步一步计算 37 × 24。' }
    )
    stream = $false
} | ConvertTo-Json -Depth 10
```

思考模式通常比普通对话更慢。网页是否展示、允许返回多少思考内容，以及最终输出格式，都可能随 DeepSeek 网页变化。

## 启动、停止和自动恢复

### 启动

```powershell
.\start-deepseek-dual-tab.ps1
```

启动脚本只会处理本 DeepSeek 模块记录的进程，不会停止 ChatGPT Web2API 或你的日常浏览器。双路已经运行时，重复执行只会显示当前状态；即使某个工作进程正在回答，或正在等待你处理登录、验证码、网页变化等状态，也不会为了“重启”而中断它。

确实需要主动重启时，等当前请求结束后依次执行：

```powershell
.\stop-deepseek-dual-tab.ps1
.\start-deepseek-dual-tab.ps1
```

### 正常停止

```powershell
.\stop-deepseek-dual-tab.ps1
```

请优先使用停止脚本，不要直接在任务管理器里结束所有 Chrome。停止时正在处理的请求会中断。

### 安装意外退出自动恢复

确认手动启动和双路健康检查都正常后，再执行：

```powershell
.\install-watchdog.ps1
```

守护脚本 `watch-deepseek-dual-tab.ps1` 会检查本模块的网关和两个工作进程；连续失败后，才会调用启动脚本恢复。验证码、登录过期、账号限流属于需要人工处理的状态，不应靠无限重启或重复发送绕过。

### 隐藏到系统托盘（推荐）

确认两个专用浏览器都已经登录后，执行：

    .\install-background.ps1

两个 DeepSeek 专用窗口会隐藏到右下角系统托盘，不再占用任务栏。双击 DeepSeek 托盘图标可同时显示两个窗口；右键可选择显示或隐藏。托盘控制器还会启动守护程序，误关浏览器后会按守护规则自动恢复；登录失效、验证码和风控仍需要本人手动处理。

只想退出托盘图标时，可右键选择“Exit tray icon”，两个专用窗口会自动恢复到任务栏，API 不会停止。若要取消登录自启动并退出托盘图标，执行：

    .\install-background.ps1 -Uninstall

## 目录里有哪些重要文件

```text
deepseek-web2api/
├─ src/deepseek_web2api/             DeepSeek 网页控制和 API 实现
├─ tests/                             不使用真实账号的自动化测试
├─ dual_tab_gateway.py               双路请求分配网关
├─ pyproject.toml                    Python 包与依赖定义
├─ config.example.json               不含真实值的配置模板
├─ setup-windows.ps1                 Windows 首次安装
├─ start-deepseek-dual-tab.ps1       启动双通道；运行中重复执行不会重启
├─ stop-deepseek-dual-tab.ps1        正常停止双通道
├─ watch-deepseek-dual-tab.ps1       故障检测与自动恢复
├─ install-watchdog.ps1              安装后台守护
├─ deepseek-dual-tab-tray.ps1        双窗口系统托盘显示/隐藏控制
├─ install-background.ps1            安装托盘、守护与登录自启动
├─ README.md                          从零部署与调用教程
└─ docs/TROUBLESHOOTING.md            常见错误和处理方法
```

## 隐私与上传边界

以下内容只留在你的电脑上，**绝对不要提交到 GitHub、发到群里或打包分享**：

- 两个 Chrome profile／用户数据目录
- Cookie、Local Storage、登录二维码、登录凭据和账号信息
- 含真实密钥或真实本机路径的 `config.json`
- `*.log`、运行状态文件、错误截图中的账号信息和完整响应内容
- 临时文件、缓存、会话编号以及导出的浏览器数据

只分享代码、`config.example.json` 和删除隐私后的错误摘要。排查问题前先阅读[常见问题与故障排查](docs/TROUBLESHOOTING.md)。

## 使用边界

- 只控制你本人有权使用的账号。
- 不绕过验证码、登录验证、限流、排队或风控。
- 不用于批量采集、售卖接口、共享账号或对外提供服务。
- 网页 DOM 随时可能变化；升级前先停止服务并保留可回退的代码版本。
- 需要长期稳定、正式部署或多人调用时，应改用 DeepSeek 官方 API。
