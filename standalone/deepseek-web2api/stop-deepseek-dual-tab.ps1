param(
    [switch]$KeepWatchdogActive,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeRoot = Join-Path $WorkRoot 'runtime'
$StateRoot = Join-Path $RuntimeRoot 'state'
$PauseFile = Join-Path $WorkRoot '.deepseek-watchdog-paused'
$GatewayFile = Join-Path $WorkRoot 'dual_tab_gateway.py'
$StateFiles = @(
    (Join-Path $StateRoot 'gateway.json'),
    (Join-Path $StateRoot 'worker-1.json'),
    (Join-Path $StateRoot 'worker-2.json')
)

function Test-WorkerIdentity {
    param(
        [string]$CommandLine,
        [object]$State
    )

    if (-not $CommandLine -or $State.kind -ne 'worker') { return $false }
    $profile = [string]$State.profile_dir
    if (-not $profile) { return $false }
    return $CommandLine.Contains('deepseek_web2api') -and $CommandLine.Contains($profile)
}

function Test-GatewayIdentity {
    param(
        [string]$CommandLine,
        [object]$State
    )

    if (-not $CommandLine -or $State.kind -ne 'gateway') { return $false }
    return $CommandLine.Contains($GatewayFile)
}

function Test-BrowserIdentity {
    param(
        [string]$CommandLine,
        [object]$State
    )

    if (-not $CommandLine -or $State.kind -ne 'worker') { return $false }
    $profile = [string]$State.profile_dir
    $cdpPort = [int]$State.cdp_port
    if (-not $profile -or $cdpPort -le 0) { return $false }
    $portPattern = '--remote-debugging-port(?:=|\s+)' + [regex]::Escape([string]$cdpPort) + '(?:\s|$)'
    return $CommandLine.Contains('--user-data-dir') -and
        $CommandLine.Contains($profile) -and
        $CommandLine -match $portPattern
}

function Get-ProcessByExactId {
    param([int]$ProcessId)

    if ($ProcessId -le 0) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
}

function Get-LocalListenerProcessIds {
    param([int]$Port)

    if ($Port -le 0) { return @() }
    try {
        return @(
            Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop |
                Where-Object {
                    $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::1', '::')
                } |
                Select-Object -ExpandProperty OwningProcess |
                Where-Object { [int]$_ -gt 0 } |
                ForEach-Object { [int]$_ } |
                Sort-Object -Unique
        )
    } catch {
        return @()
    }
}

function Stop-VerifiedProcess {
    param(
        [int]$ProcessId,
        [ValidateSet('worker', 'gateway', 'browser')]
        [string]$Identity,
        [object]$State
    )

    $process = Get-ProcessByExactId -ProcessId $ProcessId
    if (-not $process) { return }

    $matches = switch ($Identity) {
        'worker' { Test-WorkerIdentity -CommandLine $process.CommandLine -State $State }
        'gateway' { Test-GatewayIdentity -CommandLine $process.CommandLine -State $State }
        'browser' { Test-BrowserIdentity -CommandLine $process.CommandLine -State $State }
    }
    if (-not $matches) {
        Write-Warning "PID $ProcessId no longer matches its DeepSeek state record; it was not stopped."
        return
    }

    Stop-Process -Id $ProcessId -Force -ErrorAction Stop
    if (-not $Quiet) { Write-Output "Stopped $Identity PID $ProcessId." }
}

function Find-DedicatedBrowserPidsFromState {
    param([object]$State)

    if ($State.kind -ne 'worker') { return @() }
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            Test-BrowserIdentity -CommandLine $_.CommandLine -State $State
        } | Select-Object -ExpandProperty ProcessId
    )
}

if (-not $KeepWatchdogActive) {
    Set-Content -LiteralPath $PauseFile -Value (Get-Date).ToString('o') -Encoding UTF8
}

$states = @()
foreach ($stateFile in $StateFiles) {
    if (-not (Test-Path -LiteralPath $stateFile)) { continue }
    try {
        $state = Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json
        $states += [pscustomobject]@{ Path = $stateFile; Value = $state }
    } catch {
        Write-Warning "Ignoring unreadable state file: $stateFile"
    }
}

# Stop the Python owners first so they cannot relaunch a browser while shutting down.
foreach ($record in $states) {
    $state = $record.Value
    try {
        if ($state.kind -eq 'gateway') {
            $gatewayPids = @([int]$state.pid, [int]$state.launcher_pid) +
                @(Get-LocalListenerProcessIds -Port ([int]$state.port))
            foreach ($gatewayPid in @($gatewayPids | Where-Object { $_ -gt 0 } | Sort-Object -Unique)) {
                Stop-VerifiedProcess -ProcessId $gatewayPid -Identity gateway -State $state
            }
        } elseif ($state.kind -eq 'worker') {
            $workerPids = @([int]$state.pid, [int]$state.launcher_pid) +
                @(Get-LocalListenerProcessIds -Port ([int]$state.api_port))
            foreach ($workerPid in @($workerPids | Where-Object { $_ -gt 0 } | Sort-Object -Unique)) {
                Stop-VerifiedProcess -ProcessId $workerPid -Identity worker -State $state
            }
        }
    } catch {
        Write-Warning "Could not stop recorded $($state.kind) PID $($state.pid): $($_.Exception.Message)"
    }
}

Start-Sleep -Milliseconds 750

# A browser can appear after startup captured its PID. Re-discover only browsers
# matching the exact profile + CDP pair in that worker's state, then apply the
# same identity check again immediately before stopping the PID.
foreach ($record in $states) {
    $state = $record.Value
    if ($state.kind -ne 'worker') { continue }
    $browserPids = @($state.browser_pids) + @(Find-DedicatedBrowserPidsFromState -State $state)
    foreach ($browserPid in @($browserPids | Sort-Object -Unique)) {
        try {
            Stop-VerifiedProcess -ProcessId ([int]$browserPid) -Identity browser -State $state
        } catch {
            Write-Warning "Could not stop recorded browser PID $browserPid`: $($_.Exception.Message)"
        }
    }
}

foreach ($record in $states) {
    Remove-Item -LiteralPath $record.Path -Force -ErrorAction SilentlyContinue
}

if (-not $Quiet) {
    if ($KeepWatchdogActive) {
        Write-Output 'Recorded DeepSeek Web2API processes stopped for restart.'
    } else {
        Write-Output 'DeepSeek Web2API stopped. The watchdog is paused until the next manual start.'
    }
}
