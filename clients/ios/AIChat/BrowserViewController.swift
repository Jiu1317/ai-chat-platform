import UIKit
import WebKit

final class BrowserViewController: UIViewController {
    // Replace these example hosts with the domain used by your own deployment.
    private static let homeURL = URL(string: "https://chat.example.com/")!
    private static let allowedHosts: Set<String> = ["chat.example.com"]

    private lazy var webView: WKWebView = makeWebView()
    private let progressView = UIProgressView(progressViewStyle: .bar)
    private let offlineView = OfflineView()
    private var progressObservation: NSKeyValueObservation?
    private var downloadDestinations: [ObjectIdentifier: URL] = [:]

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        configureLayout()
        configureProgressObservation()
        loadHome()
    }

    deinit {
        progressObservation?.invalidate()
        webView.configuration.userContentController.removeScriptMessageHandler(forName: "aiShare")
    }

    private func makeWebView() -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.allowsInlineMediaPlayback = true
        configuration.mediaTypesRequiringUserActionForPlayback = []
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = true
        configuration.applicationNameForUserAgent = "AIChatIOS/1.0.3"

        let shareScript = WKUserScript(
            source: """
            (() => {
              if (window.__aiIOSReady) return;
              window.__aiIOSReady = true;
              if (!navigator.share) {
                Object.defineProperty(navigator, 'share', {
                  configurable: true,
                  value: (data = {}) => {
                    window.webkit.messageHandlers.aiShare.postMessage({
                      title: String(data.title || ''),
                      text: String(data.text || ''),
                      url: String(data.url || '')
                    });
                    return Promise.resolve();
                  }
                });
              }
            })();
            """,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: false
        )
        configuration.userContentController.addUserScript(shareScript)
        configuration.userContentController.add(WeakScriptMessageHandler(self), name: "aiShare")

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.scrollView.keyboardDismissMode = .interactive
        webView.scrollView.refreshControl = makeRefreshControl()
        webView.isOpaque = false
        webView.backgroundColor = .systemBackground
        webView.scrollView.backgroundColor = .systemBackground
        return webView
    }

    private func makeRefreshControl() -> UIRefreshControl {
        let control = UIRefreshControl()
        control.addTarget(self, action: #selector(refreshPage), for: .valueChanged)
        return control
    }

    private func configureLayout() {
        webView.translatesAutoresizingMaskIntoConstraints = false
        progressView.translatesAutoresizingMaskIntoConstraints = false
        offlineView.translatesAutoresizingMaskIntoConstraints = false
        progressView.tintColor = UIColor(red: 47 / 255, green: 129 / 255, blue: 247 / 255, alpha: 1)
        progressView.trackTintColor = .clear
        offlineView.retryButton.addTarget(self, action: #selector(retry), for: .touchUpInside)

        view.addSubview(webView)
        view.addSubview(offlineView)
        view.addSubview(progressView)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),
            offlineView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            offlineView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            offlineView.topAnchor.constraint(equalTo: view.topAnchor),
            offlineView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            progressView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            progressView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            progressView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
        ])
        offlineView.isHidden = true
        progressView.isHidden = true
    }

    private func configureProgressObservation() {
        progressObservation = webView.observe(\.estimatedProgress, options: [.new]) { [weak self] webView, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                let progress = Float(webView.estimatedProgress)
                self.progressView.setProgress(progress, animated: progress > self.progressView.progress)
                self.progressView.isHidden = progress >= 1
                if progress >= 1 { self.progressView.progress = 0 }
            }
        }
    }

    private func loadHome() {
        offlineView.isHidden = true
        webView.load(URLRequest(url: Self.homeURL, cachePolicy: .useProtocolCachePolicy, timeoutInterval: 30))
    }

    private func showOffline(message: String) {
        webView.scrollView.refreshControl?.endRefreshing()
        offlineView.messageLabel.text = message
        offlineView.isHidden = false
        view.bringSubviewToFront(offlineView)
        view.bringSubviewToFront(progressView)
    }

    private func finishNavigation() {
        webView.scrollView.refreshControl?.endRefreshing()
        offlineView.isHidden = true
    }

    @objc private func retry() {
        if let url = webView.url {
            offlineView.isHidden = true
            webView.load(URLRequest(url: url, cachePolicy: .reloadRevalidatingCacheData, timeoutInterval: 30))
        } else {
            loadHome()
        }
    }

    @objc private func refreshPage() {
        if webView.url == nil { loadHome() } else { webView.reload() }
    }

    private func isAllowed(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        return Self.allowedHosts.contains(host)
    }

    private func openExternally(_ url: URL) {
        UIApplication.shared.open(url, options: [:])
    }

    private func presentShare(items: [Any], sourceView: UIView? = nil) {
        guard !items.isEmpty else { return }
        let controller = UIActivityViewController(activityItems: items, applicationActivities: nil)
        if let popover = controller.popoverPresentationController {
            popover.sourceView = sourceView ?? view
            popover.sourceRect = sourceView?.bounds ?? CGRect(x: view.bounds.midX, y: view.bounds.midY, width: 1, height: 1)
        }
        present(controller, animated: true)
    }

    private func presentDownloadError(_ error: Error) {
        let alert = UIAlertController(title: "下载失败", message: error.localizedDescription, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "好", style: .default))
        present(alert, animated: true)
    }

    private func safeFilename(_ value: String) -> String {
        let invalid = CharacterSet(charactersIn: "/\\?%*|\"<>:")
        let cleaned = value.components(separatedBy: invalid).joined(separator: "-").trimmingCharacters(in: .whitespacesAndNewlines)
        return String((cleaned.isEmpty ? "AI-Download" : cleaned).prefix(120))
    }
}

extension BrowserViewController: WKNavigationDelegate {
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if navigationAction.shouldPerformDownload {
            decisionHandler(.download)
            return
        }
        let scheme = url.scheme?.lowercased() ?? ""
        if ["about", "blob", "data"].contains(scheme) || isAllowed(url) {
            decisionHandler(.allow)
            return
        }
        if ["http", "https", "mailto", "tel"].contains(scheme) { openExternally(url) }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let response = navigationResponse.response as? HTTPURLResponse
        let disposition = response?.value(forHTTPHeaderField: "Content-Disposition")?.lowercased() ?? ""
        decisionHandler(disposition.contains("attachment") || !navigationResponse.canShowMIMEType ? .download : .allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { finishNavigation() }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code == NSURLErrorCancelled { return }
        showOffline(message: "页面加载失败，请检查网络后重试。\n\(error.localizedDescription)")
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code == NSURLErrorCancelled { return }
        showOffline(message: "无法连接 AI Chat，请检查网络后重试。\n\(error.localizedDescription)")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }
}

extension BrowserViewController: WKUIDelegate {
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard navigationAction.targetFrame == nil, let url = navigationAction.request.url else { return nil }
        if isAllowed(url) { webView.load(navigationAction.request) } else { openExternally(url) }
        return nil
    }

    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin, initiatedBy frame: WKFrameInfo, type: WKMediaCaptureType, decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(Self.allowedHosts.contains(origin.host.lowercased()) ? .grant : .prompt)
    }
}

extension BrowserViewController: WKDownloadDelegate {
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        do {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("AIDownloads", isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let destination = directory.appendingPathComponent(safeFilename(suggestedFilename))
            try? FileManager.default.removeItem(at: destination)
            downloadDestinations[ObjectIdentifier(download)] = destination
            completionHandler(destination)
        } catch {
            completionHandler(nil)
            presentDownloadError(error)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let destination = downloadDestinations.removeValue(forKey: ObjectIdentifier(download)) else { return }
        presentShare(items: [destination])
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        downloadDestinations.removeValue(forKey: ObjectIdentifier(download))
        presentDownloadError(error)
    }
}

extension BrowserViewController: WKScriptMessageHandler {
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "aiShare", let payload = message.body as? [String: Any] else { return }
        var items: [Any] = []
        let text = (payload["text"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let title = (payload["title"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let urlText = (payload["url"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !title.isEmpty { items.append(title) }
        if !text.isEmpty { items.append(text) }
        if let url = URL(string: urlText), !urlText.isEmpty { items.append(url) }
        presentShare(items: items)
    }
}

private final class WeakScriptMessageHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?
    init(_ target: WKScriptMessageHandler) { self.target = target }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.userContentController(userContentController, didReceive: message)
    }
}

private final class OfflineView: UIView {
    let messageLabel = UILabel()
    let retryButton = UIButton(type: .system)

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .systemBackground
        let symbol = UIImageView(image: UIImage(systemName: "wifi.exclamationmark"))
        symbol.preferredSymbolConfiguration = UIImage.SymbolConfiguration(pointSize: 38, weight: .regular)
        symbol.tintColor = .secondaryLabel
        symbol.contentMode = .scaleAspectFit
        let title = UILabel()
        title.text = "暂时无法连接"
        title.font = .preferredFont(forTextStyle: .title2)
        title.adjustsFontForContentSizeCategory = true
        title.textColor = .label
        messageLabel.text = "请检查网络后重试。"
        messageLabel.font = .preferredFont(forTextStyle: .body)
        messageLabel.adjustsFontForContentSizeCategory = true
        messageLabel.textColor = .secondaryLabel
        messageLabel.textAlignment = .center
        messageLabel.numberOfLines = 0
        var buttonConfiguration = UIButton.Configuration.filled()
        buttonConfiguration.title = "重新加载"
        buttonConfiguration.cornerStyle = .medium
        retryButton.configuration = buttonConfiguration
        let stack = UIStackView(arrangedSubviews: [symbol, title, messageLabel, retryButton])
        stack.axis = .vertical
        stack.alignment = .center
        stack.spacing = 14
        stack.setCustomSpacing(20, after: messageLabel)
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: leadingAnchor, constant: 28),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -28),
            messageLabel.widthAnchor.constraint(lessThanOrEqualToConstant: 320),
            retryButton.heightAnchor.constraint(greaterThanOrEqualToConstant: 44),
        ])
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
}
