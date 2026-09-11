package com.example.aichat;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.MimeTypeMap;
import android.webkit.SslErrorHandler;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import android.net.http.SslError;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.util.ArrayDeque;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import androidx.activity.ComponentActivity;
import androidx.activity.OnBackPressedCallback;
import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;

public final class MainActivity extends ComponentActivity {
    private static final int MAX_BRIDGE_DOWNLOAD_BYTES = 30 * 1024 * 1024;
    private static final int MAX_BRIDGE_DOWNLOAD_BASE64_CHARS =
            ((MAX_BRIDGE_DOWNLOAD_BYTES + 2) / 3) * 4 + 16;

    private WebView webView;
    private ProgressBar progressBar;
    private View offlineView;
    private TextView offlineMessage;
    private ValueCallback<Uri[]> fileChooserCallback;
    private ActivityResultLauncher<Intent> fileChooserLauncher;
    private ActivityResultLauncher<String> storagePermissionLauncher;
    private final ArrayDeque<DownloadRequest> pendingDownloads = new ArrayDeque<>();
    private boolean storagePermissionRequestInFlight;
    private ExecutorService fileExecutor;
    private Uri homeUri;
    private String allowedHost;

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        homeUri = Uri.parse(getString(R.string.home_url));
        allowedHost = getString(R.string.allowed_host).trim().toLowerCase(Locale.ROOT);
        if (!"https".equalsIgnoreCase(homeUri.getScheme()) || !allowedHost.equalsIgnoreCase(homeUri.getHost())) {
            throw new IllegalStateException("home_url must use HTTPS and match allowed_host");
        }

        fileExecutor = Executors.newSingleThreadExecutor();
        webView = findViewById(R.id.web_view);
        progressBar = findViewById(R.id.progress_bar);
        offlineView = findViewById(R.id.offline_view);
        offlineMessage = findViewById(R.id.offline_message);
        findViewById(R.id.retry_button).setOnClickListener(view -> reloadCurrentPage());

        fileChooserLauncher = registerForActivityResult(
                new ActivityResultContracts.StartActivityForResult(),
                result -> {
                    if (fileChooserCallback == null) return;
                    Uri[] uris = result.getResultCode() == RESULT_OK
                            ? WebChromeClient.FileChooserParams.parseResult(
                                    result.getResultCode(),
                                    result.getData()
                            )
                            : null;
                    fileChooserCallback.onReceiveValue(uris);
                    fileChooserCallback = null;
                }
        );
        storagePermissionLauncher = registerForActivityResult(
                new ActivityResultContracts.RequestPermission(),
                granted -> {
                    storagePermissionRequestInFlight = false;
                    DownloadRequest request;
                    while ((request = pendingDownloads.pollFirst()) != null) {
                        if (granted) {
                            startDownload(request);
                        } else if (request.url.startsWith("http://") || request.url.startsWith("https://")) {
                            openExternally(Uri.parse(request.url));
                        } else {
                            Toast.makeText(this, R.string.download_failed, Toast.LENGTH_SHORT).show();
                        }
                    }
                }
        );

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setJavaScriptCanOpenWindowsAutomatically(true);
        settings.setSupportMultipleWindows(false);
        settings.setUserAgentString(settings.getUserAgentString() + " AIChatAndroid/1.0.0");

        CookieManager cookieManager = CookieManager.getInstance();
        cookieManager.setAcceptCookie(true);
        cookieManager.setAcceptThirdPartyCookies(webView, false);

        webView.setWebViewClient(new SecureWebViewClient());
        webView.setWebChromeClient(new AppWebChromeClient());
        webView.addJavascriptInterface(new ShareBridge(), "AndroidShare");
        webView.addJavascriptInterface(new DownloadBridge(), "AndroidDownload");
        webView.setDownloadListener(new AppDownloadListener());
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                if (webView.canGoBack()) {
                    webView.goBack();
                } else {
                    setEnabled(false);
                    getOnBackPressedDispatcher().onBackPressed();
                }
            }
        });

        if (savedInstanceState == null || webView.restoreState(savedInstanceState) == null) {
            loadHome();
        }
    }

    private void loadHome() {
        hideOffline();
        webView.loadUrl(homeUri.toString());
    }

    private void reloadCurrentPage() {
        hideOffline();
        if (webView.getUrl() == null) {
            loadHome();
        } else {
            webView.reload();
        }
    }

    private boolean isAllowed(Uri uri) {
        return uri != null
                && "https".equalsIgnoreCase(uri.getScheme())
                && allowedHost.equalsIgnoreCase(uri.getHost());
    }

    private void hideOffline() {
        offlineView.setVisibility(View.GONE);
        webView.setVisibility(View.VISIBLE);
    }

    private void showOffline(String detail) {
        offlineMessage.setText(getString(
                R.string.offline_message_with_detail,
                getString(R.string.offline_message),
                detail
        ));
        offlineView.setVisibility(View.VISIBLE);
        webView.setVisibility(View.INVISIBLE);
        progressBar.setVisibility(View.GONE);
    }

    private void openExternally(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException error) {
            Toast.makeText(this, R.string.no_app, Toast.LENGTH_SHORT).show();
        }
    }

    private void injectShareSupport() {
        String script = "(() => {"
                + "if (window.__aiAndroidReady) return;"
                + "window.__aiAndroidReady = true;"
                + "if (!navigator.share) Object.defineProperty(navigator, 'share', {configurable:true,value:(data={})=>{"
                + "window.AndroidShare.share(String(data.title||''),String(data.text||''),String(data.url||''));"
                + "return Promise.resolve();}});"
                + "})();";
        webView.evaluateJavascript(script, null);
    }

    private void share(String title, String text, String url) {
        StringBuilder body = new StringBuilder();
        if (title != null && !title.isBlank()) body.append(title.trim());
        if (text != null && !text.isBlank()) {
            if (body.length() > 0) body.append("\n\n");
            body.append(text.trim());
        }
        if (url != null && !url.isBlank()) {
            if (body.length() > 0) body.append("\n");
            body.append(url.trim());
        }
        if (body.length() == 0) return;
        Intent intent = new Intent(Intent.ACTION_SEND);
        intent.setType("text/plain");
        intent.putExtra(Intent.EXTRA_TEXT, body.toString());
        startActivity(Intent.createChooser(intent, getString(R.string.app_name)));
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        webView.saveState(outState);
        super.onSaveInstanceState(outState);
    }

    @Override
    protected void onStop() {
        CookieManager.getInstance().flush();
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        if (fileChooserCallback != null) fileChooserCallback.onReceiveValue(null);
        webView.removeJavascriptInterface("AndroidShare");
        webView.removeJavascriptInterface("AndroidDownload");
        webView.stopLoading();
        webView.destroy();
        pendingDownloads.clear();
        fileExecutor.shutdownNow();
        super.onDestroy();
    }

    private final class SecureWebViewClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri uri = request.getUrl();
            if (isAllowed(uri)) return false;
            String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
            if (scheme.equals("about") && "blank".equalsIgnoreCase(uri.getSchemeSpecificPart())) return false;
            if (scheme.equals("http")
                    || scheme.equals("https")
                    || scheme.equals("mailto")
                    || scheme.equals("tel")) {
                openExternally(uri);
            }
            return true;
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            hideOffline();
            progressBar.setVisibility(View.VISIBLE);
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            progressBar.setVisibility(View.GONE);
            if (isAllowed(Uri.parse(url))) injectShareSupport();
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame()) showOffline(error.getDescription().toString());
        }

        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            if (request.isForMainFrame() && response.getStatusCode() >= 500) {
                showOffline(getString(R.string.http_error, response.getStatusCode()));
            }
        }

        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            handler.cancel();
            showOffline(getString(R.string.https_error));
        }
    }

    private final class AppWebChromeClient extends WebChromeClient {
        @Override
        public void onProgressChanged(WebView view, int newProgress) {
            progressBar.setProgress(newProgress);
            progressBar.setVisibility(newProgress >= 100 ? View.GONE : View.VISIBLE);
        }

        @Override
        public boolean onShowFileChooser(
                WebView view,
                ValueCallback<Uri[]> callback,
                FileChooserParams params
        ) {
            if (fileChooserCallback != null) fileChooserCallback.onReceiveValue(null);
            fileChooserCallback = callback;
            try {
                fileChooserLauncher.launch(params.createIntent());
                return true;
            } catch (ActivityNotFoundException error) {
                fileChooserCallback = null;
                callback.onReceiveValue(null);
                Toast.makeText(MainActivity.this, R.string.no_app, Toast.LENGTH_SHORT).show();
                return false;
            }
        }
    }

    private final class AppDownloadListener implements DownloadListener {
        @Override
        public void onDownloadStart(
                String url,
                String userAgent,
                String contentDisposition,
                String mimeType,
                long contentLength
        ) {
            String fileName = safeFilename(URLUtil.guessFileName(url, contentDisposition, mimeType));
            if (contentLength > MAX_BRIDGE_DOWNLOAD_BYTES) {
                Toast.makeText(MainActivity.this, R.string.download_too_large, Toast.LENGTH_SHORT).show();
                return;
            }
            DownloadRequest request = new DownloadRequest(url, userAgent, mimeType, fileName);
            if (Build.VERSION.SDK_INT <= Build.VERSION_CODES.P
                    && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                pendingDownloads.addLast(request);
                if (!storagePermissionRequestInFlight) {
                    storagePermissionRequestInFlight = true;
                    storagePermissionLauncher.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE);
                }
                return;
            }
            startDownload(request);
        }
    }

    private void startDownload(DownloadRequest request) {
        if (request.url.startsWith("blob:")) {
            downloadBlob(request.url, request.fileName, request.mimeType);
        } else if (request.url.startsWith("data:")) {
            new DownloadBridge().saveDataUrl(request.url, request.fileName, request.mimeType);
        } else {
            enqueueDownload(request);
        }
    }

    private void enqueueDownload(DownloadRequest source) {
        try {
            DownloadManager.Request request = new DownloadManager.Request(Uri.parse(source.url));
            request.setTitle(source.fileName);
            request.setDescription(getString(R.string.app_name));
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, source.fileName);
            request.setAllowedOverMetered(true);
            request.setAllowedOverRoaming(false);
            if (source.mimeType != null && !source.mimeType.isBlank()) request.setMimeType(source.mimeType);
            if (source.userAgent != null && !source.userAgent.isBlank()) request.addRequestHeader("User-Agent", source.userAgent);
            String cookie = CookieManager.getInstance().getCookie(source.url);
            if (cookie != null && !cookie.isBlank()) request.addRequestHeader("Cookie", cookie);
            ((DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE)).enqueue(request);
            Toast.makeText(this, R.string.download_started, Toast.LENGTH_SHORT).show();
        } catch (RuntimeException error) {
            Toast.makeText(this, R.string.download_failed, Toast.LENGTH_SHORT).show();
        }
    }

    private void downloadBlob(String url, String fileName, String mimeType) {
        String script = "(async()=>{try{"
                + "const response=await fetch(" + JSONObject.quote(url) + ");"
                + "if(!response.ok)throw new Error('HTTP '+response.status);"
                + "const blob=await response.blob();"
                + "if(blob.size>" + MAX_BRIDGE_DOWNLOAD_BYTES + ")throw new Error('File is too large');"
                + "const reader=new FileReader();"
                + "reader.onloadend=()=>window.AndroidDownload.saveDataUrl(String(reader.result),"
                + JSONObject.quote(fileName) + "," + JSONObject.quote(mimeType == null ? "" : mimeType) + ");"
                + "reader.readAsDataURL(blob);"
                + "}catch(error){window.AndroidDownload.reportError(String(error));}})();";
        webView.evaluateJavascript(script, null);
    }

    private String safeFilename(String value) {
        String result = value == null ? "AI-Download" : value.replaceAll("[\\\\/:*?\"<>|]", "-").trim();
        if (result.isEmpty()) result = "AI-Download";
        return result.length() > 120 ? result.substring(0, 120) : result;
    }

    private final class ShareBridge {
        @JavascriptInterface
        public void share(String title, String text, String url) {
            runOnUiThread(() -> MainActivity.this.share(title, text, url));
        }
    }

    private final class DownloadBridge {
        @JavascriptInterface
        public void saveDataUrl(String dataUrl, String fileName, String mimeType) {
            fileExecutor.execute(() -> {
                try {
                    int comma = dataUrl.indexOf(',');
                    if (comma < 0 || !dataUrl.substring(0, comma).contains(";base64")) {
                        throw new IllegalArgumentException("Unsupported download format");
                    }
                    String payload = dataUrl.substring(comma + 1);
                    if (payload.length() > MAX_BRIDGE_DOWNLOAD_BASE64_CHARS) {
                        throw new IllegalArgumentException("File is too large");
                    }
                    byte[] bytes = android.util.Base64.decode(payload, android.util.Base64.DEFAULT);
                    if (bytes.length > MAX_BRIDGE_DOWNLOAD_BYTES) throw new IllegalArgumentException("File is too large");
                    String resolvedMime = mimeType == null || mimeType.isBlank()
                            ? dataUrl.substring(5, dataUrl.indexOf(';'))
                            : mimeType;
                    saveBytes(bytes, safeFilename(addExtensionIfNeeded(fileName, resolvedMime)), resolvedMime);
                    runOnUiThread(() -> Toast.makeText(MainActivity.this, R.string.download_saved, Toast.LENGTH_SHORT).show());
                } catch (Exception error) {
                    runOnUiThread(() -> Toast.makeText(MainActivity.this, R.string.download_failed, Toast.LENGTH_SHORT).show());
                }
            });
        }

        @JavascriptInterface
        public void reportError(String ignored) {
            runOnUiThread(() -> Toast.makeText(MainActivity.this, R.string.download_failed, Toast.LENGTH_SHORT).show());
        }
    }

    private String addExtensionIfNeeded(String fileName, String mimeType) {
        String name = fileName == null || fileName.isBlank() ? "AI-Download" : fileName;
        if (name.contains(".")) return name;
        String extension = MimeTypeMap.getSingleton().getExtensionFromMimeType(mimeType);
        return extension == null ? name : name + "." + extension;
    }

    private void saveBytes(byte[] bytes, String fileName, String mimeType) throws Exception {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ContentValues values = new ContentValues();
            values.put(MediaStore.Downloads.DISPLAY_NAME, fileName);
            values.put(MediaStore.Downloads.MIME_TYPE, mimeType);
            values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
            values.put(MediaStore.Downloads.IS_PENDING, 1);
            Uri item = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
            if (item == null) throw new IllegalStateException("Cannot create download");
            try (OutputStream output = getContentResolver().openOutputStream(item)) {
                if (output == null) throw new IllegalStateException("Cannot open download");
                output.write(bytes);
            } catch (Exception error) {
                getContentResolver().delete(item, null, null);
                throw error;
            }
            values.clear();
            values.put(MediaStore.Downloads.IS_PENDING, 0);
            getContentResolver().update(item, values, null, null);
        } else {
            File directory = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
            if (!directory.exists() && !directory.mkdirs()) throw new IllegalStateException("Cannot create download directory");
            try (OutputStream output = new FileOutputStream(new File(directory, fileName))) {
                output.write(bytes);
            }
        }
    }

    private static final class DownloadRequest {
        final String url;
        final String userAgent;
        final String mimeType;
        final String fileName;

        DownloadRequest(String url, String userAgent, String mimeType, String fileName) {
            this.url = url;
            this.userAgent = userAgent;
            this.mimeType = mimeType;
            this.fileName = fileName;
        }
    }
}
