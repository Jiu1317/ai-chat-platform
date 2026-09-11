$ErrorActionPreference = 'Stop'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $WorkRoot '.venv\Scripts\python.exe'
$ConfigFile = Join-Path $WorkRoot 'web2api-config.json'
$GatewayFile = Join-Path $WorkRoot 'dual_tab_gateway.py'
$ProcessStateFile = Join-Path $WorkRoot 'dual-tab-processes.json'
$startedProcesses = @()
$hasStartMutex = $false

function ConvertTo-ProcessArgument {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value.Contains('"')) {
        throw 'Bridge paths containing a double quote are not supported.'
    }
    return '"' + $Value + '"'
}

function Test-LocalPort {
    param([Parameter(Mandatory = $true)][int]$Port)

    $client = $null
    try {
        $client = [Net.Sockets.TcpClient]::new()
        $attempt = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $attempt.AsyncWaitHandle.WaitOne(1000)) {
            return $false
        }
        $client.EndConnect($attempt)
        return $true
    } catch {
        return $false
    } finally {
        if ($client) {
            $client.Close()
        }
    }
}

function Get-GatewayHealth {
    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(5)
    try {
        $response = $client.GetAsync('http://127.0.0.1:9181/health').GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        return $body | ConvertFrom-Json
    } finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

# urllib honors system proxy variables. CDP and worker traffic must never send
# loopback addresses through the desktop HTTP proxy.
$env:NO_PROXY = '127.0.0.1,localhost'
$env:no_proxy = '127.0.0.1,localhost'

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python environment not found. Run setup-windows.ps1 first."
}
if (-not (Test-Path -LiteralPath $ConfigFile)) {
    throw "Configuration not found. Copy web2api-config.example.json to web2api-config.json and replace the API key."
}

$ConfigArgument = ConvertTo-ProcessArgument $ConfigFile
$GatewayArgument = ConvertTo-ProcessArgument $GatewayFile
$createdNew = $false
$startMutex = [Threading.Mutex]::new(
    $false,
    'Local\AIChatWeb2APIDualTabStart',
    [ref]$createdNew
)

try {
$hasStartMutex = $startMutex.WaitOne(0)
if (-not $hasStartMutex) {
    Write-Output 'Another ChatGPT Web2API start or recovery is already running.'
    return
}

# A second start while the complete stack is healthy must not interrupt an
# in-flight request. Use stop-dual-tab.ps1 first for an intentional restart.
$runtimePorts = @(9181, 9182, 9183, 9325)
if (($runtimePorts | Where-Object { -not (Test-LocalPort -Port $_) }).Count -eq 0) {
    try {
        $existingHealth = Get-GatewayHealth
        if ($existingHealth.mode -eq 'dual-tab' -and
            [int]$existingHealth.ready_backends -eq 2) {
            Write-Output 'ChatGPT Web2API is already running with two ready workers.'
            return
        }
    } catch {
        # Continue into exact module-process cleanup when this is not a
        # readable gateway health response.
    }
}

$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and (
        $_.CommandLine -like "*$GatewayFile*" -or
        ($_.CommandLine -match 'chatgpt_web2api' -and $_.CommandLine -like "*$ConfigFile*")
    )
}
foreach ($process in $existing) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2

$worker1 = Start-Process -FilePath $PythonExe -ArgumentList @(
    '-m', 'chatgpt_web2api', '--config', $ConfigArgument,
    '--port', '9182', '--cdp-port', '9325'
) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $WorkRoot 'dual-worker-1.stdout.log') -RedirectStandardError (Join-Path $WorkRoot 'dual-worker-1.stderr.log') -PassThru
$startedProcesses += $worker1

$worker1Deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    if ($worker1.HasExited) {
        throw 'ChatGPT worker 1 exited during startup. Check dual-worker-1.stderr.log.'
    }
    $worker1Ready = Test-NetConnection 127.0.0.1 -Port 9182 -InformationLevel Quiet -WarningAction SilentlyContinue
} until ($worker1Ready -or (Get-Date) -gt $worker1Deadline)

if (-not $worker1Ready) {
    throw 'ChatGPT worker 1 did not become ready within 30 seconds.'
}

$worker2 = Start-Process -FilePath $PythonExe -ArgumentList @(
    '-m', 'chatgpt_web2api', '--config', $ConfigArgument,
    '--port', '9183', '--cdp-port', '9325'
) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $WorkRoot 'dual-worker-2.stdout.log') -RedirectStandardError (Join-Path $WorkRoot 'dual-worker-2.stderr.log') -PassThru
$startedProcesses += $worker2

$deadline = (Get-Date).AddSeconds(45)
do {
    Start-Sleep -Milliseconds 500
    if ($worker2.HasExited) {
        throw 'ChatGPT worker 2 exited during startup. Check dual-worker-2.stderr.log.'
    }
    $ready1 = Test-NetConnection 127.0.0.1 -Port 9182 -InformationLevel Quiet -WarningAction SilentlyContinue
    $ready2 = Test-NetConnection 127.0.0.1 -Port 9183 -InformationLevel Quiet -WarningAction SilentlyContinue
} until (($ready1 -and $ready2) -or (Get-Date) -gt $deadline)

if (-not ($ready1 -and $ready2)) {
    throw 'The two ChatGPT workers did not become ready within 45 seconds.'
}

$gateway = Start-Process -FilePath $PythonExe -ArgumentList @(
    $GatewayArgument, '--host', '127.0.0.1', '--port', '9181',
    '--backends', 'http://127.0.0.1:9182', 'http://127.0.0.1:9183'
) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $WorkRoot 'dual-gateway.stdout.log') -RedirectStandardError (Join-Path $WorkRoot 'dual-gateway.stderr.log') -PassThru
$startedProcesses += $gateway

@{
    gateway = $gateway.Id
    worker1 = $worker1.Id
    worker2 = $worker2.Id
    started_at = (Get-Date).ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath $ProcessStateFile -Encoding UTF8

Start-Sleep -Seconds 2
if ($gateway.HasExited) {
    throw 'Dual-tab gateway exited during startup. Check dual-gateway.stderr.log.'
}
$health = Invoke-RestMethod -Uri 'http://127.0.0.1:9181/health' -TimeoutSec 10
if ($health.ready_backends -ne 2) {
    throw "Dual-tab gateway started, but only $($health.ready_backends) backend(s) are ready."
}

Write-Output "Dual-tab gateway ready: gateway=$($gateway.Id), workers=$($worker1.Id),$($worker2.Id)"
} catch {
    foreach ($startedProcess in $startedProcesses) {
        try {
            Stop-Process -Id $startedProcess.Id -Force -ErrorAction SilentlyContinue
        } catch {
            # Continue cleanup so one failed stop cannot leave the other
            # processes from this startup attempt running.
        }
    }
    Remove-Item -LiteralPath $ProcessStateFile -Force -ErrorAction SilentlyContinue
    throw
} finally {
    if ($hasStartMutex) {
        try {
            $startMutex.ReleaseMutex()
        } catch {
            # The original startup error remains the useful diagnostic.
        }
    }
    $startMutex.Dispose()
}
