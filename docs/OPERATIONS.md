# 日常使用与排错

## 网站服务

查看状态：

```bash
sudo systemctl status ai-chat --no-pager
```

重启：

```bash
sudo systemctl restart ai-chat
```

查看最近日志：

```bash
sudo journalctl -u ai-chat -n 100 --no-pager
```

检查 Nginx：

```bash
sudo nginx -t
sudo systemctl status nginx --no-pager
```

## 更新网站

先在仓库中获取新版本，再运行更新脚本：

```bash
git pull
sudo bash scripts/update-server.sh
```

脚本不会覆盖 `/etc/ai-chat/website.env`、账户数据库或用户项目。图片桥接如果原本正在运行，脚本会先停止网站接收新请求，等待在途任务完成后再更新并恢复两个服务；因此有图片正在生成时，更新命令可能会等待一段时间。更新中途失败时，脚本会尝试恢复网站和图片桥接，并保留原错误信息，方便继续排查。

## Windows 双路 API

启动：

```powershell
.\start-dual-tab.ps1
```

服务已经健康运行时，重复执行启动脚本只会显示“已在运行”，不会中断正在传输的回答。确实需要重启时，请先执行停止脚本，等它结束后再启动。

停止：

```powershell
.\stop-dual-tab.ps1
```

检查：

```powershell
Invoke-RestMethod http://127.0.0.1:9181/health
```

如果 `ready_backends` 小于 2：

1. 确认专用 Chrome 仍处于登录状态。
2. 关闭卡住的回答。
3. 再次运行 `start-dual-tab.ps1`。
4. 查看 `dual-worker-1.stderr.log` 和 `dual-worker-2.stderr.log`。

双路服务一次处理两个请求，默认最多再等待 32 个请求；队列已满时会立即提示稍后重试，避免请求无限堆积。单个工作进程健康检查超过 5 秒会被暂时视为不可用，其他健康进程仍可继续工作。

## 回答长时间显示处理中

Pro 或联网模型可能需要几分钟。接口会发送无内容保活信号，不应因为等待而出现网络错误。真正的最终答案只会在网页端完成后返回。

如果超过 420 秒仍没有结果：

1. 查看专用标签页是否弹出验证或限额提示。
2. 点击网页端停止按钮。
3. 重启双路 API。
4. 换普通模型测试，判断是否只是 Pro 模型繁忙。

## Cloudflare Tunnel

前台运行：

```powershell
.\start-cloudflare-tunnel.ps1
```

如果公网健康检查失败，先确认本机健康地址正常，再检查配置中的 Tunnel UUID、凭据路径和域名是否一致。

## 安全备份

需要备份的是服务器上的：

```text
/srv/ai-chat/data
/srv/ai-chat/workspaces
/etc/ai-chat
```

其中包含账户、密钥和聊天内容。备份必须加密，不能提交到 GitHub。
