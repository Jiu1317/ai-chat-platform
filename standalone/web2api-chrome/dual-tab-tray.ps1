$ErrorActionPreference = 'Continue'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$WatchScript = Join-Path $WorkRoot 'watch-dual-tab.ps1'
$ExitSignalFile = Join-Path $WorkRoot '.dual-tab-tray-exit'
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

function Get-DedicatedBrowserProcessIds {
    $processIds = @()

    try {
        $processIds += @(
            Get-NetTCPConnection -State Listen -LocalPort $CdpPort -ErrorAction Stop |
                Select-Object -ExpandProperty OwningProcess
        )
    } catch {
        # Fall back to the browser command line when the TCP table is unavailable.
    }

    $portPattern = '--remote-debugging-port(?:=|\s+)' +
        [regex]::Escape([string]$CdpPort) + '(?:\s|$)'
    $processIds += @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -match 'chrome|msedge' -and
            $_.CommandLine -and
            $_.CommandLine -match $portPattern -and
            $_.CommandLine -notmatch '(?:^|\s)--type='
        } | Select-Object -ExpandProperty ProcessId
    )

    return @(
        $processIds |
            Where-Object { [int]$_ -gt 0 } |
            ForEach-Object { [int]$_ } |
            Sort-Object -Unique
    )
}

function ConvertTo-NativeArgument {
    param([string]$Value)

    if ($Value.Contains('"')) { throw 'Paths containing a double quote are not supported.' }
    if ($Value -match '\s') { return '"' + $Value + '"' }
    return $Value
}

function Hide-DedicatedBrowser {
    foreach ($processId in @(Get-DedicatedBrowserProcessIds)) {
        [void][AIChatWeb2APITrayWindowApi]::HideForProcess($processId)
    }
}

function Show-DedicatedBrowser {
    foreach ($processId in @(Get-DedicatedBrowserProcessIds)) {
        [void][AIChatWeb2APITrayWindowApi]::ShowForProcess($processId)
    }
}

function Ensure-Watchdog {
    # Older copies may live in a different folder but monitor the same fixed
    # 9181/9182/9183/9325 stack. Recognize the script name as well as this
    # exact path so installing a newer tray does not create two recoverers.
    $watchdogCommandPattern = '(?i)(?:^|[\\/])watch-dual-tab\.ps1(?:"|\s|$)'
    $existing = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and
        ($_.CommandLine.Contains($WatchScript) -or
            $_.CommandLine -match $watchdogCommandPattern) -and
        $_.ProcessId -ne $PID
    }
    if (-not $existing) {
        Start-Process -FilePath 'powershell.exe' -ArgumentList @(
            '-NoProfile', '-WindowStyle', 'Hidden',
            '-ExecutionPolicy', 'Bypass', '-File', (ConvertTo-NativeArgument $WatchScript)
        ) -WindowStyle Hidden
    }
}

$createdNew = $false
$mutex = [System.Threading.Mutex]::new($true, 'Local\AIChatWeb2APIDualTabTray', [ref]$createdNew)
if (-not $createdNew) {
    $mutex.Dispose()
    exit 0
}

if (Test-Path -LiteralPath $ExitSignalFile) {
    try {
        Remove-Item -LiteralPath $ExitSignalFile -Force -ErrorAction SilentlyContinue
        Show-DedicatedBrowser
    } finally {
        try { $mutex.ReleaseMutex() } catch {}
        $mutex.Dispose()
    }
    exit 0
}
Ensure-Watchdog

$browserProcess = $null
foreach ($processId in @(Get-DedicatedBrowserProcessIds)) {
    try {
        $candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction Stop
        if ($candidate.ExecutablePath) {
            $browserProcess = $candidate
            break
        }
    } catch {
        # The default icon is sufficient when the executable path is unavailable.
    }
}
$trayIcon = [System.Windows.Forms.NotifyIcon]::new()
$trayIcon.Text = 'ChatGPT Web2API dedicated browser'
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
    $script:keepHidden = $false
    Show-DedicatedBrowser
    $trayIcon.Visible = $false
    $context.ExitThread()
})

$timer = [System.Windows.Forms.Timer]::new()
$timer.Interval = 2000
$timer.add_Tick({
    if (Test-Path -LiteralPath $ExitSignalFile) {
        Remove-Item -LiteralPath $ExitSignalFile -Force -ErrorAction SilentlyContinue
        $script:keepHidden = $false
        Show-DedicatedBrowser
        $trayIcon.Visible = $false
        $context.ExitThread()
        return
    }

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
    Show-DedicatedBrowser
    Remove-Item -LiteralPath $ExitSignalFile -Force -ErrorAction SilentlyContinue
    $trayIcon.Visible = $false
    $trayIcon.Dispose()
    $menu.Dispose()
    $context.Dispose()
    try { $mutex.ReleaseMutex() } catch {}
    $mutex.Dispose()
}
