# Android 客户端

这是一个不含真实域名和密钥的 Android WebView 客户端模板。它支持登录状态保存、文件上传、普通文件与 Blob 文件下载、系统分享、返回上一页、外部链接跳转、加载进度和断网重试。

## 小白修改域名

用文本编辑器打开：

```text
app/src/main/res/values/strings.xml
```

找到下面两行：

```xml
<string name="home_url" translatable="false">https://chat.example.com/</string>
<string name="allowed_host" translatable="false">chat.example.com</string>
```

把 `chat.example.com` 换成自己的网站域名。必须使用 HTTPS，两行的域名必须一致。不要在源码中填写网站账号、密码或 API 密钥。

## 使用 Android Studio 编译

1. 安装最新版稳定版 Android Studio。
2. 点击 **Open**，选择本 `clients/android` 文件夹。
3. 等待右下角 Gradle 同步和依赖下载完成。
4. 如果软件提示安装 Android 37 SDK，点击提示中的 **Install** 并等待完成。
5. 在顶部设备列表选择自己的安卓手机或模拟器。
6. 点击绿色运行按钮，即可安装到测试设备。
7. 需要 APK 文件时，依次点击 **Build → Build App Bundle(s) / APK(s) → Build APK(s)**。

调试 APK 默认位于：

```text
app/build/outputs/apk/debug/app-debug.apk
```

## 使用命令编译

安装好 JDK 17 和 Android SDK 后，在本目录执行：

```powershell
.\gradlew.bat lintDebug assembleDebug
```

macOS 或 Linux 执行：

```bash
./gradlew lintDebug assembleDebug
```

## 从 GitHub Actions 下载测试 APK

每次修改 `clients/android/` 并推送到 GitHub 后，仓库的 **Actions → Build Android APK** 会自动检查并编译示例 APK。打开成功的运行记录，在 **Artifacts** 中下载 `AIChat-Android-debug`。

注意：仓库中的自动构建使用示例域名，只用于验证源码能够编译。要得到连接自己网站的 APK，请先在本地替换域名，再用 Android Studio 编译。

## 安装提示

- 自己测试可以安装调试 APK，不要求上架应用商店。
- 手机可能要求允许“安装未知应用”，只给你信任的文件管理器临时授权。
- 给其他人长期分发前，应创建自己的签名密钥并生成 release APK。
- 签名文件和密码属于机密，不能提交到 GitHub。
