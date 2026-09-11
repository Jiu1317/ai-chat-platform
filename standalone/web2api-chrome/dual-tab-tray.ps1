$ErrorActionPreference = 'Continue'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$WatchScript = Join-Path $WorkRoot 'watch-dual-tab.ps1'
$CdpPort = 9325

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class AIChatWeb2APITrayWindowApi
{
    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("user32.dll")]
    private static extern bool ShowWindowAsync(IntPtr hWnd, int command);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern int GetWindowTextLength(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern IntPtr GetWindow(IntPtr hWnd, uint command);

    private const uint GW_OWNER = 4;

    public static int HideForProcess(int processId)
    {
        int changed = 0;
        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam)
        {
            uint windowProcessId;
            GetWindowThreadProcessId(hWnd, out windowProcessId);
            if (windowProcessId == processId &&
                GetWindow(hWnd, GW_OWNER) == IntPtr.Zero &&
                GetWindowTextLength(hWnd) > 0)
            {
                if (ShowWindowAsync(hWnd, 0)) changed++;
            }
            return true;
        }, IntPtr.Zero);
        return changed;
    }

    public static int ShowForProcess(int processId)
    {
        int changed = 0;
        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam)
        {
            uint windowProcessId;
            GetWindowThreadProcessId(hWnd, out windowProcessId);
            if (windowProcessId == processId &&
                GetWindow(hWnd, GW_OWNER) == IntPtr.Zero &&
                GetWindowTextLength(hWnd) > 0)
            {
                ShowWindowAsync(hWnd, 9);
                SetForegroundWindow(hWnd);
                changed++;
            }
            return true;
        }, IntPtr.Zero);
        return changed;
    }
}
"@

function Get-DedicatedBrowserProcess {
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match 'chrome|msedge' -and
        $_.CommandLine -and
        $_.CommandLine -match "--remote-debugging-port=$CdpPort(?:\s|$)"
    } | Select-Object -First 1
}

function Hide-DedicatedBrowser {
    $browser = Get-DedicatedBrowserProcess
    if ($browser) {
        [void][AIChatWeb2APITrayWindowApi]::HideForProcess([int]$browser.ProcessId)
    }
}

function Show-DedicatedBrowser {
    $browser = Get-DedicatedBrowserProcess
    if ($browser) {
        [void][AIChatWeb2APITrayWindowApi]::ShowForProcess([int]$browser.ProcessId)
    }
}

function Ensure-Watchdog {
    $existing = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and
        $_.CommandLine -like "*$WatchScript*" -and
        $_.ProcessId -ne $PID
    }
    if (-not $existing) {
        Start-Process -FilePath 'powershell.exe' -ArgumentList @(
            '-NoProfile', '-WindowStyle', 'Hidden',
            '-ExecutionPolicy', 'Bypass', '-File', $WatchScript
        ) -WindowStyle Hidden
    }
}

$createdNew = $false
$mutex = [System.Threading.Mutex]::new($true, 'Local\AIChatWeb2APIDualTabTray', [ref]$createdNew)
if (-not $createdNew) {
    $mutex.Dispose()
    exit 0
}

Ensure-Watchdog

$browserProcess = Get-DedicatedBrowserProcess
$trayIcon = [System.Windows.Forms.NotifyIcon]::new()
$trayIcon.Text = 'AI Chat Web2API dedicated browser'
if ($browserProcess -and $browserProcess.ExecutablePath) {
    try {
        $trayIcon.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon($browserProcess.ExecutablePath)
    } catch {
        $trayIcon.Icon = [System.Drawing.SystemIcons]::Application
    }
} else {
    $trayIcon.Icon = [System.Drawing.SystemIcons]::Application
}

$menu = [System.Windows.Forms.ContextMenuStrip]::new()
$showItem = $menu.Items.Add('Show dedicated browser')
$hideItem = $menu.Items.Add('Hide dedicated browser')
[void]$menu.Items.Add('-')
$exitItem = $menu.Items.Add('Exit tray icon')
$trayIcon.ContextMenuStrip = $menu
$trayIcon.Visible = $true

$script:keepHidden = $true
Hide-DedicatedBrowser

$showItem.add_Click({
    $script:keepHidden = $false
    Show-DedicatedBrowser
})
$hideItem.add_Click({
    $script:keepHidden = $true
    Hide-DedicatedBrowser
})
$trayIcon.add_DoubleClick({
    $script:keepHidden = $false
    Show-DedicatedBrowser
})

$context = [System.Windows.Forms.ApplicationContext]::new()
$exitItem.add_Click({
    $trayIcon.Visible = $false
    $context.ExitThread()
})

$timer = [System.Windows.Forms.Timer]::new()
$timer.Interval = 2000
$timer.add_Tick({
    Ensure-Watchdog
    if ($script:keepHidden) {
        Hide-DedicatedBrowser
    }
})
$timer.Start()

try {
    [System.Windows.Forms.Application]::Run($context)
} finally {
    $timer.Stop()
    $timer.Dispose()
    $trayIcon.Visible = $false
    $trayIcon.Dispose()
    $menu.Dispose()
    $context.Dispose()
    try { $mutex.ReleaseMutex() } catch {}
    $mutex.Dispose()
}
