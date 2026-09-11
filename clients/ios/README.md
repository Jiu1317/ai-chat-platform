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

上传大小由网页端统一校验；客户端会在下载前或下载完成后再次执行单文件 30 MiB 上限。

## 方法一：有 Mac 时直接安装

1. 用 Xcode 打开 `AIChat.xcodeproj`。
2. 在项目的 `Signing & Capabilities` 中选择自己的 Apple 账号团队。
3. 如果 Xcode 提示包名已被占用，把 `Bundle Identifier` 改成只属于你的值，例如 `com.example.aichat.jiujiu`。
4. 用数据线连接 iPhone，选择这台 iPhone 作为运行设备，然后点击运行按钮。
5. 若手机提示开发者未受信任，按系统提示在“设置”中启用开发者模式并信任对应证书。

## 只有 Windows 时怎么办

Windows 不能直接运行 Xcode，因此不能在本机编译这个 iOS 工程。当前仓库也没有提供 iOS 云构建工作流；不要在 Actions 中寻找不存在的 `Build unsigned iOS IPA`。

可行方法是使用自己的 Mac、可信的远程 Mac，或自行配置带 macOS 构建机的 CI。无论采用哪种方式，最终仍需用自己的 Apple ID 或开发者证书签名后才能安装到 iPhone。未签名 IPA 不能直接在手机上点击安装；个人免费签名可能需要定期重新签名，这是 iOS 的限制，不是应用故障。

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
