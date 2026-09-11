param(
    [switch]$FromWatchdog
)

$ErrorActionPreference = 'Stop'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeRoot = Join-Path $WorkRoot 'runtime'
$StateRoot = Join-Path $RuntimeRoot 'state'
$LogRoot = Join-Path $RuntimeRoot 'logs'
$DefaultPython = Join-Path $WorkRoot '.venv\Scripts\python.exe'
$PythonExe = if ($env:DEEPSEEK_WEB2API_PYTHON) { $env:DEEPSEEK_WEB2API_PYTHON } else { $DefaultPython }
$ConfigFile = if ($env:DEEPSEEK_WEB2API_CONFIG) { $env:DEEPSEEK_WEB2API_CONFIG } else { Join-Path $WorkRoot 'config.json' }
$ProfileRoot = if ($env:DEEPSEEK_WEB2API_PROFILE_ROOT) { $env:DEEPSEEK_WEB2API_PROFILE_ROOT } else { Join-Path $RuntimeRoot 'profiles' }
$GatewayFile = Join-Path $WorkRoot 'dual_tab_gateway.py'
$StopScript = Join-Path $WorkRoot 'stop-deepseek-dual-tab.ps1'
$PauseFile = Join-Path $WorkRoot '.deepseek-watchdog-paused'

function Get-EnvironmentInt {
    param(
        [string]$Name,
        [int]$Default,
        [int]$Minimum = 1
    )

    $raw = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) { return $Default }
    $parsed = 0
    if (-not [int]::TryParse($raw, [ref]$parsed) -or $parsed -lt $Minimum) {
        throw "$Name must be an integer greater than or equal to $Minimum."
    }
    return $parsed
}

function Get-EnvironmentDouble {
    param(
        [string]$Name,
        [double]$Default
    )

    $raw = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) { return $Default }
    $parsed = 0.0
    if (-not [double]::TryParse(
        $raw,
        [Globalization.NumberStyles]::Float,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$parsed
    ) -or $parsed -le 0) {
        throw "$Name must be a positive number."
    }
    return $parsed
}

$GatewayPort = Get-EnvironmentInt -Name 'DEEPSEEK_GATEWAY_PORT' -Default 9191
$Worker1Port = Get-EnvironmentInt -Name 'DEEPSEEK_WORKER1_PORT' -Default 9192
$Worker2Port = Get-EnvironmentInt -Name 'DEEPSEEK_WORKER2_PORT' -Default 9193
$Worker1CdpPort = Get-EnvironmentInt -Name 'DEEPSEEK_WORKER1_CDP_PORT' -Default 9332
$Worker2CdpPort = Get-EnvironmentInt -Name 'DEEPSEEK_WORKER2_CDP_PORT' -Default 9333
$QueueTimeout = Get-EnvironmentDouble -Name 'DEEPSEEK_GATEWAY_QUEUE_TIMEOUT' -Default 60
$MaxQueued = Get-EnvironmentInt -Name 'DEEPSEEK_GATEWAY_MAX_QUEUED' -Default 32 -Minimum 0
$MaxRequestBytes = Get-EnvironmentInt -Name 'DEEPSEEK_GATEWAY_MAX_REQUEST_BYTES' -Default (30 * 1024 * 1024)
$StartupTimeout = Get-EnvironmentInt -Name 'DEEPSEEK_STARTUP_TIMEOUT_SECONDS' -Default 75
$Profile1 = [IO.Path]::GetFullPath((Join-Path $ProfileRoot 'worker-1'))
$Profile2 = [IO.Path]::GetFullPath((Join-Path $ProfileRoot 'worker-2'))
$GatewayStateFile = Join-Path $StateRoot 'gateway.json'
$Worker1StateFile = Join-Path $StateRoot 'worker-1.json'
$Worker2StateFile = Join-Path $StateRoot 'worker-2.json'

function ConvertTo-NativeArgument {
    param([string]$Value)

    if ($Value.Contains('"')) { throw 'Paths containing a double quote are not supported.' }
    if ($Value -match '\s') { return '"' + $Value + '"' }
    return $Value
}

function Test-LocalPort {
    param([int]$Port)

    $client = $null
    try {
        $client = [Net.Sockets.TcpClient]::new()
        $attempt = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $attempt.AsyncWaitHandle.WaitOne(1000)) { return $false }
        $client.EndConnect($attempt)
        return $true
    } catch {
        return $false
    } finally {
        if ($client) { $client.Close() }
    }
}

function Get-LocalListenerProcessId {
    param([int]$Port)

    try {
        $owners = @(
            Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop |
                Where-Object {
                    $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::1', '::')
                } |
                Select-Object -ExpandProperty OwningProcess
        )
        if ($owners.Count -gt 0) {
            return [int]($owners | Sort-Object -Unique | Select-Object -First 1)
        }
    } catch {
        # The caller can still use the launcher PID when the TCP table is unavailable.
    }
    return 0
}

function Wait-LocalPort {
    param(
        [int]$Port,
        [datetime]$Deadline
    )

    do {
        if (Test-LocalPort -Port $Port) { return $true }
        Start-Sleep -Milliseconds 500
    } until ((Get-Date) -ge $Deadline)
    return $false
}

function Write-StateFile {
    param(
        [string]$Path,
        [hashtable]$State
    )

    $temporary = "$Path.$PID.tmp"
    $State | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Find-DedicatedBrowserPids {
    param(
        [string]$Profile,
        [int]$CdpPort
    )

    $processIds = @()
    $listenerPid = Get-LocalListenerProcessId -Port $CdpPort
    if ($listenerPid -gt 0) { $processIds += $listenerPid }

    $portPattern = '--remote-debugging-port(?:=|\s+)' + [regex]::Escape([string]$CdpPort) + '(?:\s|$)'
    $processIds += @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.Name -match '^(chrome|msedge)\.exe$' -and
            $_.CommandLine -and
            $_.CommandLine.Contains('--user-data-dir') -and
            $_.CommandLine.Contains($Profile) -and
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

function Get-GatewayHealth {
    param([int]$Port)

    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(5)
    try {
        $response = $client.GetAsync("http://127.0.0.1:$Port/health").GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        return $body | ConvertFrom-Json
    } finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

$createdNew = $false
$mutex = [Threading.Mutex]::new($false, 'Local\DeepSeekWeb2APIDualWorkerStart', [ref]$createdNew)
$hasMutex = $false
$startedProcesses = @()

try {
    $hasMutex = $mutex.WaitOne(0)
    if (-not $hasMutex) {
        Write-Output 'Another DeepSeek Web2API start or recovery is already running.'
        exit 0
    }

    if (-not $FromWatchdog) {
        Remove-Item -LiteralPath $PauseFile -Force -ErrorAction SilentlyContinue
    }

    foreach ($requiredFile in @($PythonExe, $ConfigFile, $GatewayFile, $StopScript)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "Required file not found: $requiredFile"
        }
    }

    $configText = Get-Content -Raw -LiteralPath $ConfigFile
    $null = $configText | ConvertFrom-Json
    if ($configText -match 'REPLACE_WITH_A_LONG_RANDOM_API_KEY|CHANGE[-_ ]ME') {
        throw 'config.json still contains an API-key placeholder. Replace it first.'
    }

    $allPorts = @($GatewayPort, $Worker1Port, $Worker2Port, $Worker1CdpPort, $Worker2CdpPort)
    if (($allPorts | Sort-Object -Unique).Count -ne $allPorts.Count) {
        throw 'Gateway, worker, and CDP ports must all be distinct.'
    }
    if (($allPorts | Where-Object { $_ -gt 65535 }).Count -gt 0) {
        throw 'Gateway, worker, and CDP ports must not exceed 65535.'
    }
    if ($Profile1 -eq $Profile2) { throw 'Worker profile directories must be distinct.' }

    New-Item -ItemType Directory -Path $StateRoot, $LogRoot, $Profile1, $Profile2 -Force | Out-Null
    $env:NO_PROXY = '127.0.0.1,localhost'
    $env:no_proxy = '127.0.0.1,localhost'

    # Re-running Start while the complete stack is already reachable must be
    # harmless. This also protects an in-flight request: a busy worker can be
    # reported as temporarily not-ready even though it must not be restarted.
    $expectedRuntimePorts = @(
        $GatewayPort, $Worker1Port, $Worker2Port, $Worker1CdpPort, $Worker2CdpPort
    )
    if (($expectedRuntimePorts | Where-Object { -not (Test-LocalPort -Port $_) }).Count -eq 0) {
        try {
            $existingHealth = Get-GatewayHealth -Port $GatewayPort
            $leaveRunningStatuses = @(
                'ok',
                'authentication_required',
                'login_required',
                'captcha_required',
                'rate_limited',
                'dom_changed',
                'send_unknown'
            )
            $stableBackends = @(
                @($existingHealth.backends) | Where-Object {
                    $_.reachable -eq $true -and
                    $leaveRunningStatuses -contains ([string]$_.status).Trim().ToLowerInvariant()
                }
            )
            if ($existingHealth.mode -eq 'deepseek-dual-worker' -and
                $stableBackends.Count -eq 2) {
                Write-Output (
                    "DeepSeek Web2API is already running at http://127.0.0.1:$GatewayPort " +
                    "(ready workers: $($existingHealth.ready_backends)/2)."
                )
                if ([int]$existingHealth.ready_backends -lt 2) {
                    Write-Warning 'A browser worker is busy or needs manual attention; it was left running.'
                }
                return
            }
        } catch {
            # A listener that cannot return this module's health response is
            # handled by the owned-process cleanup and exact port checks below.
        }
    }

    # Stop only PIDs previously recorded by this module. Unknown port owners are never killed.
    & $StopScript -KeepWatchdogActive -Quiet
    Start-Sleep -Milliseconds 500
    foreach ($port in $allPorts) {
        if (Test-LocalPort -Port $port) {
            throw "Local port $port is already in use by an unowned process."
        }
    }

    $worker1Arguments = @(
        '-m', 'deepseek_web2api',
        '--config', (ConvertTo-NativeArgument $ConfigFile),
        '--host', '127.0.0.1',
        '--port', [string]$Worker1Port,
        '--cdp-port', [string]$Worker1CdpPort,
        '--profile-dir', (ConvertTo-NativeArgument $Profile1)
    )
    $worker1 = Start-Process -FilePath $PythonExe -ArgumentList $worker1Arguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogRoot 'worker-1.stdout.log') `
        -RedirectStandardError (Join-Path $LogRoot 'worker-1.stderr.log') `
        -PassThru
    $startedProcesses += $worker1
    Write-StateFile -Path $Worker1StateFile -State @{
        kind = 'worker'; worker = 1; pid = $worker1.Id; launcher_pid = $worker1.Id
        api_port = $Worker1Port
        cdp_port = $Worker1CdpPort; profile_dir = $Profile1; browser_pids = @()
        started_at = (Get-Date).ToString('o')
    }

    $worker2Arguments = @(
        '-m', 'deepseek_web2api',
        '--config', (ConvertTo-NativeArgument $ConfigFile),
        '--host', '127.0.0.1',
        '--port', [string]$Worker2Port,
        '--cdp-port', [string]$Worker2CdpPort,
        '--profile-dir', (ConvertTo-NativeArgument $Profile2)
    )
    $worker2 = Start-Process -FilePath $PythonExe -ArgumentList $worker2Arguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogRoot 'worker-2.stdout.log') `
        -RedirectStandardError (Join-Path $LogRoot 'worker-2.stderr.log') `
        -PassThru
    $startedProcesses += $worker2
    Write-StateFile -Path $Worker2StateFile -State @{
        kind = 'worker'; worker = 2; pid = $worker2.Id; launcher_pid = $worker2.Id
        api_port = $Worker2Port
        cdp_port = $Worker2CdpPort; profile_dir = $Profile2; browser_pids = @()
        started_at = (Get-Date).ToString('o')
    }

    $workerDeadline = (Get-Date).AddSeconds($StartupTimeout)
    $worker1Listening = Wait-LocalPort -Port $Worker1Port -Deadline $workerDeadline
    $worker2Listening = Wait-LocalPort -Port $Worker2Port -Deadline $workerDeadline
    if (-not ($worker1Listening -and $worker2Listening)) {
        throw "The two worker APIs did not start within $StartupTimeout seconds."
    }

    $worker1ListenerPid = Get-LocalListenerProcessId -Port $Worker1Port
    $worker2ListenerPid = Get-LocalListenerProcessId -Port $Worker2Port
    if ($worker1ListenerPid -le 0 -or $worker2ListenerPid -le 0) {
        throw 'A worker port opened, but its owning process could not be identified.'
    }

    $browserCaptureDeadline = (Get-Date).AddSeconds([Math]::Min(10, $StartupTimeout))
    do {
        $worker1BrowserPids = Find-DedicatedBrowserPids -Profile $Profile1 -CdpPort $Worker1CdpPort
        $worker2BrowserPids = Find-DedicatedBrowserPids -Profile $Profile2 -CdpPort $Worker2CdpPort
        if ($worker1BrowserPids.Count -gt 0 -and $worker2BrowserPids.Count -gt 0) { break }
        Start-Sleep -Milliseconds 500
    } until ((Get-Date) -ge $browserCaptureDeadline)
    Write-StateFile -Path $Worker1StateFile -State @{
        kind = 'worker'; worker = 1; pid = $worker1ListenerPid; launcher_pid = $worker1.Id
        api_port = $Worker1Port
        cdp_port = $Worker1CdpPort; profile_dir = $Profile1
        browser_pids = @($worker1BrowserPids); started_at = (Get-Date).ToString('o')
    }
    Write-StateFile -Path $Worker2StateFile -State @{
        kind = 'worker'; worker = 2; pid = $worker2ListenerPid; launcher_pid = $worker2.Id
        api_port = $Worker2Port
        cdp_port = $Worker2CdpPort; profile_dir = $Profile2
        browser_pids = @($worker2BrowserPids); started_at = (Get-Date).ToString('o')
    }

    $gatewayArguments = @(
        (ConvertTo-NativeArgument $GatewayFile),
        '--host', '127.0.0.1',
        '--port', [string]$GatewayPort,
        '--backends', "http://127.0.0.1:$Worker1Port", "http://127.0.0.1:$Worker2Port",
        '--queue-timeout', $QueueTimeout.ToString([Globalization.CultureInfo]::InvariantCulture),
        '--max-queued', [string]$MaxQueued,
        '--max-request-bytes', [string]$MaxRequestBytes
    )
    $gateway = Start-Process -FilePath $PythonExe -ArgumentList $gatewayArguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogRoot 'gateway.stdout.log') `
        -RedirectStandardError (Join-Path $LogRoot 'gateway.stderr.log') `
        -PassThru
    $startedProcesses += $gateway
    Write-StateFile -Path $GatewayStateFile -State @{
        kind = 'gateway'; pid = $gateway.Id; launcher_pid = $gateway.Id; port = $GatewayPort
        started_at = (Get-Date).ToString('o')
    }

    $gatewayDeadline = (Get-Date).AddSeconds(20)
    if (-not (Wait-LocalPort -Port $GatewayPort -Deadline $gatewayDeadline)) {
        throw 'The local DeepSeek gateway did not start within 20 seconds.'
    }

    $gatewayListenerPid = Get-LocalListenerProcessId -Port $GatewayPort
    if ($gatewayListenerPid -le 0) {
        throw 'The gateway port opened, but its owning process could not be identified.'
    }
    Write-StateFile -Path $GatewayStateFile -State @{
        kind = 'gateway'; pid = $gatewayListenerPid; launcher_pid = $gateway.Id
        port = $GatewayPort; started_at = (Get-Date).ToString('o')
    }

    try {
        $health = Get-GatewayHealth -Port $GatewayPort
        Write-Output "DeepSeek Web2API is running at http://127.0.0.1:$GatewayPort (ready workers: $($health.ready_backends)/2)."
        if ([int]$health.ready_backends -lt 2) {
            Write-Warning 'One or both dedicated browser profiles need attention; open them and complete login or verification.'
        }
    } catch {
        Write-Warning "The gateway is listening, but its first health check was not readable: $($_.Exception.Message)"
    }
} catch {
    # Capture any browser that appeared during a partially completed startup so
    # rollback can still stop it by an exact state PID and identity check.
    try {
        if ($worker1) {
            $worker1BrowserPids = Find-DedicatedBrowserPids -Profile $Profile1 -CdpPort $Worker1CdpPort
            Write-StateFile -Path $Worker1StateFile -State @{
                kind = 'worker'; worker = 1; pid = $worker1.Id; launcher_pid = $worker1.Id
                api_port = $Worker1Port
                cdp_port = $Worker1CdpPort; profile_dir = $Profile1
                browser_pids = @($worker1BrowserPids); started_at = (Get-Date).ToString('o')
            }
        }
        if ($worker2) {
            $worker2BrowserPids = Find-DedicatedBrowserPids -Profile $Profile2 -CdpPort $Worker2CdpPort
            Write-StateFile -Path $Worker2StateFile -State @{
                kind = 'worker'; worker = 2; pid = $worker2.Id; launcher_pid = $worker2.Id
                api_port = $Worker2Port
                cdp_port = $Worker2CdpPort; profile_dir = $Profile2
                browser_pids = @($worker2BrowserPids); started_at = (Get-Date).ToString('o')
            }
        }
    } catch {}
    try { & $StopScript -KeepWatchdogActive -Quiet } catch {}
    foreach ($process in $startedProcesses) {
        try { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
    throw
} finally {
    if ($hasMutex) {
        try { $mutex.ReleaseMutex() } catch {}
    }
    $mutex.Dispose()
}
