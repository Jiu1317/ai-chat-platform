$ErrorActionPreference = 'Continue'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeRoot = Join-Path $WorkRoot 'runtime'
$StateRoot = Join-Path $RuntimeRoot 'state'
$LogRoot = Join-Path $RuntimeRoot 'logs'
$StartScript = Join-Path $WorkRoot 'start-deepseek-dual-tab.ps1'
$PauseFile = Join-Path $WorkRoot '.deepseek-watchdog-paused'
$WatchdogStateFile = Join-Path $StateRoot 'watchdog.json'
$LogFile = Join-Path $LogRoot 'watchdog.log'
$PreviousLogFile = Join-Path $LogRoot 'watchdog.previous.log'

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

$GatewayPort = Get-EnvironmentInt -Name 'DEEPSEEK_GATEWAY_PORT' -Default 9191
$Worker1Port = Get-EnvironmentInt -Name 'DEEPSEEK_WORKER1_PORT' -Default 9192
$Worker2Port = Get-EnvironmentInt -Name 'DEEPSEEK_WORKER2_PORT' -Default 9193
$Worker1CdpPort = Get-EnvironmentInt -Name 'DEEPSEEK_WORKER1_CDP_PORT' -Default 9332
$Worker2CdpPort = Get-EnvironmentInt -Name 'DEEPSEEK_WORKER2_CDP_PORT' -Default 9333
$WatchIntervalSeconds = Get-EnvironmentInt -Name 'DEEPSEEK_WATCH_INTERVAL_SECONDS' -Default 5
$FailureThreshold = Get-EnvironmentInt -Name 'DEEPSEEK_WATCH_FAILURE_THRESHOLD' -Default 2
$MaxRecoveryAttempts = Get-EnvironmentInt -Name 'DEEPSEEK_WATCH_MAX_RECOVERIES' -Default 5

$env:NO_PROXY = '127.0.0.1,localhost'
$env:no_proxy = '127.0.0.1,localhost'
New-Item -ItemType Directory -Path $StateRoot, $LogRoot -Force | Out-Null

function Write-WatchLog {
    param([string]$Message)
    try {
        if ((Test-Path -LiteralPath $LogFile) -and
            (Get-Item -LiteralPath $LogFile).Length -gt 1MB) {
            Move-Item -LiteralPath $LogFile -Destination $PreviousLogFile -Force
        }
        Add-Content -LiteralPath $LogFile `
            -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" -Encoding UTF8
    } catch {
        # Recovery must not depend on logging.
    }
}

function Test-LocalPort {
    param([int]$Port)
    $client = $null
    try {
        $client = [Net.Sockets.TcpClient]::new()
        $attempt = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $attempt.AsyncWaitHandle.WaitOne(1200)) { return $false }
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
    $client.Timeout = [TimeSpan]::FromSeconds(5)
    try {
        $response = $client.GetAsync("http://127.0.0.1:$GatewayPort/health").GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        return $body | ConvertFrom-Json
    } finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-WorkerCondition {
    param(
        [int]$Worker,
        [int]$ApiPort,
        [int]$CdpPort,
        [object]$Health
    )

    $backend = @($Health.backends) |
        Where-Object { [int]$_.worker -eq $Worker } |
        Select-Object -First 1
    if (-not $backend -or
        $backend.reachable -ne $true -or
        -not (Test-LocalPort -Port $ApiPort)) {
        return 'unavailable'
    }

    $status = ([string]$backend.status).Trim().ToLowerInvariant()
    $manualStatuses = @(
        'authentication_required',
        'login_required',
        'captcha_required',
        'rate_limited',
        'dom_changed',
        'send_unknown'
    )
    $needsManualAction = $manualStatuses -contains $status
    if (-not $needsManualAction -and
        $backend.cdp_connected -eq $true -and
        $backend.logged_in -eq $false) {
        $needsManualAction = $true
    }

    # Even a manual status cannot mask a crashed/missing CDP process.
    if (-not (Test-LocalPort -Port $CdpPort)) { return 'unavailable' }
    if ($needsManualAction) { return 'manual_action' }
    if ($status -ne 'ok' -or $backend.cdp_connected -ne $true) {
        return 'unavailable'
    }
    return 'healthy'
}

function Get-RuntimeCondition {
    if (-not (Test-LocalPort -Port $GatewayPort)) { return 'unavailable' }
    try {
        $health = Get-GatewayHealth
    } catch {
        return 'unavailable'
    }

    if ($health.mode -ne 'deepseek-dual-worker') {
        return 'unavailable'
    }

    $worker1Condition = Get-WorkerCondition -Worker 1 -ApiPort $Worker1Port `
        -CdpPort $Worker1CdpPort -Health $health
    $worker2Condition = Get-WorkerCondition -Worker 2 -ApiPort $Worker2Port `
        -CdpPort $Worker2CdpPort -Health $health

    if ($worker1Condition -eq 'unavailable' -or $worker2Condition -eq 'unavailable') {
        if ($worker1Condition -eq 'manual_action' -or $worker2Condition -eq 'manual_action') {
            return 'unavailable_with_manual_peer'
        }
        return 'unavailable'
    }
    if ($worker1Condition -eq 'manual_action' -or $worker2Condition -eq 'manual_action') {
        return 'manual_action'
    }
    return 'healthy'
}

$createdNew = $false
$mutex = [Threading.Mutex]::new($false, 'Local\DeepSeekWeb2APIDualWorkerWatchdog', [ref]$createdNew)
$hasMutex = $false

try {
    $hasMutex = $mutex.WaitOne(0)
    if (-not $hasMutex) { exit 0 }

    @{
        kind = 'watchdog'
        pid = $PID
        script = $MyInvocation.MyCommand.Path
        started_at = (Get-Date).ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $WatchdogStateFile -Encoding UTF8

    Write-WatchLog 'Watchdog started.'
    $consecutiveFailures = 0
    $recoveryAttempts = 0
    $nextRecovery = [datetime]::MinValue
    $lastCondition = ''

    while ($true) {
        if (Test-Path -LiteralPath $PauseFile) {
            if ($lastCondition -ne 'paused') { Write-WatchLog 'Watchdog is paused.' }
            $lastCondition = 'paused'
            $consecutiveFailures = 0
            Start-Sleep -Seconds $WatchIntervalSeconds
            continue
        }

        $condition = Get-RuntimeCondition
        if ($condition -ne $lastCondition) {
            Write-WatchLog "Runtime condition: $condition."
            if ($condition -eq 'unavailable_with_manual_peer') {
                Write-WatchLog 'One worker needs manual attention while its peer is unavailable; bounded whole-stack recovery is required.'
            }
            $lastCondition = $condition
        }

        if ($condition -eq 'healthy') {
            $consecutiveFailures = 0
            $recoveryAttempts = 0
            $nextRecovery = [datetime]::MinValue
            Start-Sleep -Seconds $WatchIntervalSeconds
            continue
        }

        if ($condition -eq 'manual_action') {
            # Browser login, CAPTCHA, rate limiting, and selector drift are not fixed by restarts.
            $consecutiveFailures = 0
            Start-Sleep -Seconds $WatchIntervalSeconds
            continue
        }

        $consecutiveFailures++
        if ($consecutiveFailures -lt $FailureThreshold -or (Get-Date) -lt $nextRecovery) {
            Start-Sleep -Seconds $WatchIntervalSeconds
            continue
        }

        if ($recoveryAttempts -ge $MaxRecoveryAttempts) {
            Set-Content -LiteralPath $PauseFile `
                -Value 'Automatic recovery limit reached; inspect runtime/logs before restarting.' `
                -Encoding UTF8
            Write-WatchLog 'Automatic recovery limit reached; watchdog paused for manual inspection.'
            $lastCondition = 'paused'
            continue
        }

        $recoveryAttempts++
        Write-WatchLog "Starting recovery attempt $recoveryAttempts of $MaxRecoveryAttempts."
        try {
            $startOutput = & $StartScript -FromWatchdog 2>&1
            foreach ($line in @($startOutput)) { Write-WatchLog ([string]$line) }
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
    Remove-Item -LiteralPath $WatchdogStateFile -Force -ErrorAction SilentlyContinue
    if ($hasMutex) {
        try { $mutex.ReleaseMutex() } catch {}
    }
    $mutex.Dispose()
}
