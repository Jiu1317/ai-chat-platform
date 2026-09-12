$ErrorActionPreference = 'Continue'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$StartScript = Join-Path $WorkRoot 'start-dual-tab.ps1'
$PauseFile = Join-Path $WorkRoot '.dual-tab-watchdog-paused'
$LogFile = Join-Path $WorkRoot 'dual-tab-watchdog.log'
$OldLogFile = Join-Path $WorkRoot 'dual-tab-watchdog.previous.log'

function Get-EnvironmentInt {
    param([string]$Name, [int]$Default, [int]$Minimum = 1)

    $raw = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) { return $Default }
    $parsed = 0
    if (-not [int]::TryParse($raw, [ref]$parsed) -or $parsed -lt $Minimum) {
        return $Default
    }
    return $parsed
}

$WatchIntervalSeconds = Get-EnvironmentInt -Name 'WEB2API_WATCH_INTERVAL_SECONDS' -Default 5
$FailureThreshold = Get-EnvironmentInt -Name 'WEB2API_WATCH_FAILURE_THRESHOLD' -Default 6
$MaxRecoveryAttempts = Get-EnvironmentInt -Name 'WEB2API_WATCH_MAX_RECOVERIES' -Default 5
$IndeterminateThreshold = Get-EnvironmentInt `
    -Name 'WEB2API_WATCH_INDETERMINATE_THRESHOLD' -Default 360

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

function Get-GatewayHealth {
    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(8)
    $response = $null
    try {
        # HttpClient does not throw merely because /health returns 503, so the
        # watchdog can still distinguish a known broken worker from an
        # unreadable response and can retain any reported busy transfer.
        $response = $client.GetAsync('http://127.0.0.1:9181/health').GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        return $body | ConvertFrom-Json
    } catch {
        return $null
    } finally {
        if ($response) { $response.Dispose() }
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-RuntimeCondition {
    $ports = @{
        gateway = Test-LocalPort -Port 9181
        worker1 = Test-LocalPort -Port 9182
        worker2 = Test-LocalPort -Port 9183
        chrome = Test-LocalPort -Port 9325
    }

    $health = if ($ports.gateway) { Get-GatewayHealth } else { $null }
    if ($health -and $health.mode -eq 'dual-tab') {
        # The queue size is the authoritative indication that a text, image,
        # or file-backed request currently owns a worker. Never tear down the
        # healthy peer just because the other worker failed during that turn.
        if ($health.busy -eq $true -or
            [int]$health.available_workers -lt [int]$health.capacity -or
            [int]$health.queued_requests -gt 0 -or
            [int]$health.active_asset_transfers -gt 0) {
            return 'busy'
        }

        $stableBackends = @(
            @($health.backends) | Where-Object {
                $_.reachable -eq $true -and
                $_.chrome_running -eq $true -and
                $_.cdp_connected -eq $true
            }
        )
        if ($stableBackends.Count -eq 2 -and
            $ports.worker1 -and $ports.worker2 -and $ports.chrome) {
            # Breakers, login prompts, and rate limits can make ready_backends
            # temporarily less than two. Restarting cannot fix those states
            # and can destroy an otherwise healthy browser transfer.
            return 'healthy'
        }
        return 'unavailable'
    }

    if ($ports.gateway -and $ports.worker1 -and $ports.worker2 -and $ports.chrome) {
        # All owned listeners still exist but the health call was inconclusive.
        # A long synchronous browser operation can briefly cause this. Leave
        # the processes intact rather than guessing that they are dead.
        return 'indeterminate'
    }

    return 'unavailable'
}

$createdNew = $false
$mutex = [System.Threading.Mutex]::new($true, 'Local\AIChatWeb2APIDualTabWatchdog', [ref]$createdNew)
if (-not $createdNew) {
    $mutex.Dispose()
    exit 0
}

Write-WatchLog 'Watchdog started.'
$consecutiveFailures = 0
$recoveryAttempts = 0
$nextRecovery = [datetime]::MinValue
$lastCondition = ''
$indeterminateChecks = 0

try {
    while ($true) {
        if (Test-Path -LiteralPath $PauseFile) {
            if ($lastCondition -ne 'paused') { Write-WatchLog 'Watchdog is paused.' }
            $lastCondition = 'paused'
            $consecutiveFailures = 0
            $indeterminateChecks = 0
            Start-Sleep -Seconds $WatchIntervalSeconds
            continue
        }

        $condition = Get-RuntimeCondition
        if ($condition -ne $lastCondition) {
            Write-WatchLog "Runtime condition: $condition."
            $lastCondition = $condition
        }

        if ($condition -in @('healthy', 'busy')) {
            $consecutiveFailures = 0
            $indeterminateChecks = 0
            if ($condition -eq 'healthy') {
                $recoveryAttempts = 0
                $nextRecovery = [datetime]::MinValue
            }
            Start-Sleep -Seconds $WatchIntervalSeconds
            continue
        }

        if ($condition -eq 'indeterminate') {
            $indeterminateChecks++
            $consecutiveFailures = 0
            if ($indeterminateChecks -lt $IndeterminateThreshold) {
                Start-Sleep -Seconds $WatchIntervalSeconds
                continue
            }
            Write-WatchLog 'Health remained inconclusive beyond its configured grace period; treating the runtime as unavailable.'
            $condition = 'unavailable'
        } else {
            $indeterminateChecks = 0
        }

        $consecutiveFailures++
        if ($consecutiveFailures -lt $FailureThreshold -or (Get-Date) -lt $nextRecovery) {
            Start-Sleep -Seconds $WatchIntervalSeconds
            continue
        }

        if ($recoveryAttempts -ge $MaxRecoveryAttempts) {
            Set-Content -LiteralPath $PauseFile `
                -Value 'Automatic recovery limit reached; inspect the watchdog and worker logs.' `
                -Encoding UTF8
            Write-WatchLog 'Automatic recovery limit reached; watchdog paused for manual inspection.'
            $lastCondition = 'paused'
            continue
        }

        $recoveryAttempts++
        Write-WatchLog "Confirmed runtime failure; starting recovery attempt $recoveryAttempts of $MaxRecoveryAttempts."
        try {
            $startOutput = & $StartScript -FromWatchdog 2>&1
            foreach ($line in @($startOutput)) {
                Write-WatchLog ([string]$line)
            }
        } catch {
            Write-WatchLog "Recovery failed: $($_.Exception.Message)"
        }

        $consecutiveFailures = 0
        $delay = [Math]::Min(300, 20 * [Math]::Pow(2, $recoveryAttempts - 1))
        $nextRecovery = (Get-Date).AddSeconds($delay)
        Start-Sleep -Seconds $WatchIntervalSeconds
    }
} finally {
    Write-WatchLog 'Watchdog stopped.'
    try { $mutex.ReleaseMutex() } catch {}
    $mutex.Dispose()
}
