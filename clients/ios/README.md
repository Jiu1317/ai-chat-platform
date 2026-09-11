# AI Chat iOS

这是网站的 iPhone/iPad 客户端外壳。示例应用打开 `https://chat.example.com/`；部署前请替换成你自己的域名。它沿用网站账号、聊天、项目和模型配置，不会另建一套数据。

## 已包含

- 与安卓包一致的网站入口和应用图标
- 网站登录状态与 Cookie 持久保存
- 文件/照片选择与上传
- 图片和其他附件下载后调用 iOS 系统保存/分享面板
- 网页分享按钮接入 iOS 系统分享面板
- 同站链接留在应用内，外部网页交给默认浏览器
- 下拉刷新、加载进度和断网重试
- iPhone、iPad 与深色模式兼容

## 方法一：有 Mac 时直接安装

1. 用 Xcode 打开 `AIChat.xcodeproj`。
2. 在项目的 `Signing & Capabilities` 中选择自己的 Apple 账号团队。
3. 如果 Xcode 提示包名已被占用，把 `Bundle Identifier` 改成只属于你的值，例如 `com.example.aichat.jiujiu`。
4. 用数据线连接 iPhone，选择这台 iPhone 作为运行设备，然后点击运行按钮。
5. 若手机提示开发者未受信任，按系统提示在“设置”中启用开发者模式并信任对应证书。

## 方法二：只有 Windows 时安装

项目带有 `.github/workflows/build-unsigned-ipa.yml`，可在 GitHub 的 macOS 构建机上生成未签名 IPA：

1. 在 GitHub 新建一个私人仓库，把本目录全部上传到仓库根目录。
2. 打开仓库的 `Actions`，选择 `Build unsigned iOS IPA`，点击 `Run workflow`。
3. 构建完成后，从该次任务的 `Artifacts` 下载 `AI-Chat-iOS-v1.0.3-unsigned`。
4. 解压得到 IPA，再用 Sideloadly 或 AltStore 和你自己的 Apple ID 签名并安装到 iPhone。

未签名 IPA 不能直接在 iPhone 上点击安装。个人签名可能需要定期重新签名；这是 iOS 的安装限制，不是应用故障。无需上架 App Store。

## 修改网站地址或版本

- 网站地址：`AIChat/BrowserViewController.swift` 顶部的 `homeURL`
- 允许留在应用内打开的域名：同文件顶部的 `allowedHosts`
- 应用版本：Xcode 项目的 `MARKETING_VERSION`
- 包名：Xcode 项目的 `PRODUCT_BUNDLE_IDENTIFIER`

## 当前工程参数

- 应用名：AI Chat
- 包名：`com.example.aichat`
- 版本：`1.0.3`
- 最低系统：iOS 15
- 设备：iPhone / iPad
