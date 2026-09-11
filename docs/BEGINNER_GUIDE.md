# 小白从零部署教程

这份教程不要求你会编程。按照顺序操作即可。所有 `example.com`、`YOUR_*` 和 `CHANGE_ME` 都是示例，必须换成你自己的值。

## 1. 先认识两台机器

完整功能由两部分组成：

1. **Ubuntu 云服务器**：运行网站、账户系统、数据库和可选图片桥接。
2. **Windows 电脑**：打开两个专用 ChatGPT 标签页，并把它们转换成双路 OpenAI 兼容 API。

只部署云服务器也能使用网站和普通 API 连接。需要 ChatGPT 网页模型时，再部署 Windows 双路桥接。

## 2. 需要准备什么

- 一台 Ubuntu 22.04 或更新版本的云服务器，建议至少 2 核 4GB。
- 一个自己的域名，并能修改 DNS。
- 一台能长期联网的 Windows 10/11 电脑。
- Windows 上安装 Git、Python 3.11+、Google Chrome。
- 一个可以正常使用的 ChatGPT 账户。
- 可选：Cloudflare 账户，用于把 Windows 本地 API 安全发布到公网。

任何密钥都不要发给别人，也不要提交到 GitHub。

## 3. 下载项目

在云服务器登录 SSH 后执行：

```bash
git clone YOUR_GITHUB_REPOSITORY_URL ai-chat-platform
cd ai-chat-platform
```

`YOUR_GITHUB_REPOSITORY_URL` 是你自己的仓库地址。私有仓库需要先按 GitHub 页面提示完成身份验证。

## 4. 配置域名解析

假设你准备用 `chat.example.com` 访问网站：

1. 打开域名的 DNS 控制台。
2. 添加一条 `A` 记录。
3. 名称填写 `chat`。
4. 内容填写你的云服务器公网 IP。
5. 第一次申请证书时先关闭代理，只保留 DNS 解析。

等待几分钟后，在自己的电脑运行：

```powershell
nslookup chat.example.com
```

看到的是你自己的服务器 IP，才继续下一步。

## 5. 一键安装网站

在仓库目录执行：

```bash
sudo bash scripts/install-server.sh chat.example.com
```

把 `chat.example.com` 换成你自己的域名。脚本会自动完成：

- 安装 Python、Nginx 等依赖。
- 创建独立的低权限系统用户。
- 创建虚拟环境并安装网站依赖。
- 生成随机管理员密码和会话密钥。
- 安装并启动 systemd 服务。
- 写入不包含第三方管理控制台的 Nginx 配置。

脚本结束时会显示一次初始管理员用户名和密码。请立即保存在密码管理器中。

检查网站服务：

```bash
sudo systemctl status ai-chat --no-pager
curl http://127.0.0.1:13002/healthz
```

看到 `active (running)` 和正常健康响应即可。

## 6. 开启 HTTPS

安装证书工具：

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d chat.example.com
```

仍然要替换成你的域名。完成后打开：

```text
https://chat.example.com/
```

如果使用 Cloudflare，现在可以把网站 DNS 记录切换为代理状态，并将 SSL/TLS 模式设为“完全（严格）”。

### 启用稳定的参考图传输

为了避免多张参考图被 Base64 放大后触发 524，请打开网站环境文件：

```bash
sudo nano /etc/ai-chat/website.env
```

增加下面两行，把域名换成你自己的网站域名，并把第二行替换成 `openssl rand -hex 32` 生成的随机值：

```dotenv
AI_CHAT_PUBLIC_BASE_URL=https://chat.example.com
AI_CHAT_TRANSFER_SECRET=CHANGE_ME_RANDOM_64_HEX_CHARACTERS
```

保存后执行 `sudo systemctl restart ai-chat`。临时图片链接默认 15 分钟后失效，真实域名和密钥不能提交到 GitHub。全新安装会自动生成这些配置，只有旧版本升级时需要手动补一次。


## 7. 第一次进入网站

1. 使用安装脚本显示的管理员用户名和密码登录。
2. 进入网站设置。
3. 先修改管理员密码。
4. 创建一个测试子账户；创建时可设置自动停用时长，并选择“小时后”或“天后”，填写 0 表示长期有效。
5. 给测试账户设置每小时消息上限。
6. 创建后也可以点击成员后的“到期停用”，重新输入小时数或天数；到期后该成员会退出所有设备并无法再次登录。
7. 成员行会显示剩余天数，48 小时以内改为显示剩余小时数。需要延长时重新设置期限，需要长期保留时点击“取消期限”。
8. 用测试账户登录另一个浏览器，确认看不到管理员的聊天、项目和文件。

每个账户的数据都会存放在独立空间中。不要复制或上传服务器上的 `data/` 和 `workspaces/` 目录。

倒计时从保存时刻开始，可精确设置为几小时或几天。管理员账户不能设置自动停用期限；已经到期的成员需要先设置新期限或取消期限，再点击“启用”才能重新登录。

## 8. 添加普通 API 连接

管理员进入“网站设置”，选择“添加连接”：

1. 选择 OpenAI 兼容或 Anthropic 协议。
2. 输入连接名称。
3. 输入服务商提供的 API 地址。
4. 输入你自己的 API 密钥。
5. 点击“保存并检测模型”。
6. 关闭不希望子账户使用的模型。

密钥只保存在服务器的数据目录中，页面重新打开后只显示“已保存”。

## 9. 在 Windows 部署双路 ChatGPT API

项目需要在 Windows 电脑上再下载一份。打开 PowerShell，先执行：

```powershell
git clone YOUR_GITHUB_REPOSITORY_URL ai-chat-platform
cd ai-chat-platform
```

如果刚才已经在 Windows 下载过项目，就只需要进入那个项目文件夹。接着执行：

```powershell
cd bridge\windows
Set-ExecutionPolicy -Scope Process Bypass
.\setup-windows.ps1
```

安装完成后生成一个随机 API 密钥：

```powershell
[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLower()
```

打开 `web2api-config.json`，把 `CHANGE_ME_TO_A_LONG_RANDOM_VALUE` 替换为刚生成的值。不要把这个文件提交到 GitHub。

启动双路服务：

```powershell
.\start-dual-tab.ps1
```

第一次运行会使用独立浏览器资料目录。请在打开的 Chrome 中登录你自己的 ChatGPT 账户，然后再次运行启动脚本。

检查双路状态：

```powershell
Invoke-RestMethod http://127.0.0.1:9181/health
```

正常结果应包含：

```text
status: healthy
capacity: 2
ready_backends: 2
```

这代表最多同时处理两个网页模型请求；多出来的请求会排队，不是六路并发。

## 10. 使用 Cloudflare Tunnel 发布本机 API

安装 `cloudflared` 后，在 PowerShell 执行：

```powershell
cloudflared tunnel login
cloudflared tunnel create ai-chat-api
cloudflared tunnel route dns ai-chat-api api.example.com
```

最后一个域名要换成你准备给 API 使用的子域名。

复制配置模板：

```powershell
Copy-Item .\cloudflared-config.example.yml .\cloudflared-config.yml
```

打开 `cloudflared-config.yml`，填写刚创建的 Tunnel UUID、凭据文件路径和自己的 API 子域名，然后启动：

```powershell
.\start-cloudflare-tunnel.ps1
```

测试：

```powershell
Invoke-RestMethod https://api.example.com/health
```

返回 `capacity: 2` 和 `ready_backends: 2` 才算成功。

然后在网站管理员设置中添加连接：

- API 地址：`https://api.example.com/v1`
- 协议：OpenAI 兼容
- API 密钥：`web2api-config.json` 中你自己生成的值

## 11. 可选：开启图片生成桥接

图片功能依赖 OpenClaw 的 `image_generate` 工具。先安装官方支持的 Node.js 24 和当前 OpenClaw：

```bash
curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g openclaw@latest --allow-scripts=openclaw
```

确认 `node -v` 至少为 24.16，并执行 `openclaw onboard` 按照向导完成模型授权。官方安装说明：<https://docs.openclaw.ai/install>。

创建独立图片代理：

```bash
sudo -u ai-chat -H openclaw agents add image-generator \
  --workspace /srv/ai-chat/state/workspace-image \
  --model openai/gpt-5.6-luna \
  --non-interactive
```

在 OpenClaw 中配置一个可用的图片模型。官方说明：<https://docs.openclaw.ai/tools/image-generation>。可以使用自己的图片 API 密钥，或按官方支持方式完成 OAuth。

复制图片桥接配置：

```bash
sudo cp deploy/env/image-bridge.env.example /etc/ai-chat/image-bridge.env
sudo chmod 600 /etc/ai-chat/image-bridge.env
sudo nano /etc/ai-chat/image-bridge.env
```

执行下面命令生成新 Token：

```bash
openssl rand -hex 32
```

把新 Token 同时填写到：

- `/etc/ai-chat/image-bridge.env` 的 `AI_CHAT_IMAGE_BRIDGE_TOKEN`
- `/etc/ai-chat/website.env` 的 `AI_CHAT_IMAGE_BRIDGE_TOKEN`

确认 `OPENCLAW_BIN` 与 `command -v openclaw` 的输出一致，然后启动：

```bash
sudo cp deploy/systemd/ai-chat-image-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-chat-image-bridge
sudo systemctl restart ai-chat
```

回到网站发送“用 Image 2.5 画一只猫”进行测试。

## 12. 可选：编译 Android 客户端

1. 在电脑安装最新版稳定版 Android Studio。
2. 用 Android Studio 打开 `clients/android` 文件夹。
3. 打开 `app/src/main/res/values/strings.xml`。
4. 将 `home_url` 和 `allowed_host` 中的示例域名替换为自己的网站域名，两处必须一致并使用 HTTPS。
5. 等待 Gradle 同步完成，连接安卓手机后点击绿色运行按钮。
6. 需要 APK 时，点击 **Build → Build App Bundle(s) / APK(s) → Build APK(s)**。

生成的调试 APK 位于：

```text
clients/android/app/build/outputs/apk/debug/app-debug.apk
```

完整操作和 GitHub Actions 下载方法见 [Android 客户端教程](../clients/android/README.md)。

## 13. 可选：编译 iOS 客户端

1. 在 Mac 上用 Xcode 打开 `clients/ios/AIChat.xcodeproj`。
2. 打开 `BrowserViewController.swift`。
3. 把示例域名替换为你自己的网站域名。
4. 在 Signing & Capabilities 选择自己的 Apple ID 团队。
5. 连接 iPhone，点击运行即可安装，不要求上架 App Store。

## 14. 部署后的安全检查

- GitHub 仓库中只能存在 `*.example` 配置。
- 网站和 API 必须使用 HTTPS。
- 公网 API 必须要求 API 密钥。
- 管理员和每个子账户使用不同密码。
- 定期备份服务器的 `data/` 与 `workspaces/`，但不要上传到 GitHub。
- Windows 专用 Chrome 资料目录不要用于日常浏览，也不要分享。

完成后请阅读 [日常使用与排错](OPERATIONS.md)。
