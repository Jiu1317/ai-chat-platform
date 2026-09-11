$ErrorActionPreference = 'Continue'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartScript = Join-Path $WorkRoot 'start-dual-tab.ps1'
$PauseFile = Join-Path $WorkRoot '.dual-tab-watchdog-paused'
$LogFile = Join-Path $WorkRoot 'dual-tab-watchdog.log'
$OldLogFile = Join-Path $WorkRoot 'dual-tab-watchdog.previous.log'

$env:NO_PROXY = '127.0.0.1,localhost'
$env:no_proxy = '127.0.0.1,localhost'

function Write-WatchLog {
    param([string]$Message)

    try {
        if ((Test-Path -LiteralPath $LogFile) -and
            (Get-Item -LiteralPath $LogFile).Length -gt 1MB) {
            Move-Item -LiteralPath $LogFile -Destination $OldLogFile -Force
        }
        Add-Content -LiteralPath $LogFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" -Encoding UTF8
    } catch {
        # Logging must never stop recovery.
    }
}

function Test-LocalPort {
    param([int]$Port)

    $client = $null
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $attempt = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $attempt.AsyncWaitHandle.WaitOne(1500)) {
            return $false
        }
        $client.EndConnect($attempt)
        return $true
    } catch {
        return $false
    } finally {
        if ($client) { $client.Close() }
    }
}

function Test-DualTabHealthy {
    if (-not (Test-LocalPort -Port 9325)) {
        return $false
    }
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:9181/health' -TimeoutSec 5
        return $health.status -eq 'healthy' -and $health.ready_backends -eq 2
    } catch {
        return $false
    }
}

$createdNew = $false
$mutex = [System.Threading.Mutex]::new($true, 'Local\AIChatWeb2APIDualTabWatchdog', [ref]$createdNew)
if (-not $createdNew) {
    $mutex.Dispose()
    exit 0
}

Write-WatchLog 'Watchdog started.'
$consecutiveFailures = 0

try {
    while ($true) {
        if (Test-Path -LiteralPath $PauseFile) {
            $consecutiveFailures = 0
            Start-Sleep -Seconds 5
            continue
        }

        if (Test-DualTabHealthy) {
            $consecutiveFailures = 0
            Start-Sleep -Seconds 5
            continue
        }

        $consecutiveFailures++
        if ($consecutiveFailures -lt 2) {
            Start-Sleep -Seconds 5
            continue
        }

        Write-WatchLog 'Dedicated browser or API became unavailable; starting recovery.'
        try {
            $startOutput = & $StartScript 2>&1
            foreach ($line in @($startOutput)) {
                Write-WatchLog ([string]$line)
            }
            Write-WatchLog 'Recovery completed.'
        } catch {
            Write-WatchLog "Recovery failed: $($_.Exception.Message)"
        }

        $consecutiveFailures = 0
        Start-Sleep -Seconds 20
    }
} finally {
    Write-WatchLog 'Watchdog stopped.'
    try { $mutex.ReleaseMutex() } catch {}
    $mutex.Dispose()
}
