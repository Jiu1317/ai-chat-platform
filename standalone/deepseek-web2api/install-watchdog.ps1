param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$WatchScript = Join-Path $WorkRoot 'watch-deepseek-dual-tab.ps1'
$RuntimeRoot = Join-Path $WorkRoot 'runtime'
$StateRoot = Join-Path $RuntimeRoot 'state'
$WatchdogStateFile = Join-Path $StateRoot 'watchdog.json'
$PauseFile = Join-Path $WorkRoot '.deepseek-watchdog-paused'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunName = 'DeepSeekWeb2APIWatchdog'

function Stop-RecordedWatchdog {
    if (-not (Test-Path -LiteralPath $WatchdogStateFile)) { return }
    try {
        $state = Get-Content -Raw -LiteralPath $WatchdogStateFile | ConvertFrom-Json
        if ($state.kind -ne 'watchdog' -or [int]$state.pid -le 0) { return }
        $process = Get-CimInstance Win32_Process `
            -Filter "ProcessId = $([int]$state.pid)" -ErrorAction SilentlyContinue
        if ($process -and $process.CommandLine -and $process.CommandLine.Contains($WatchScript)) {
            Stop-Process -Id ([int]$state.pid) -Force
        }
    } finally {
        Remove-Item -LiteralPath $WatchdogStateFile -Force -ErrorAction SilentlyContinue
    }
}

if ($Uninstall) {
    Set-Content -LiteralPath $PauseFile -Value (Get-Date).ToString('o') -Encoding UTF8
    Remove-ItemProperty -Path $RunKey -Name $RunName -ErrorAction SilentlyContinue
    Stop-RecordedWatchdog
    Write-Output 'DeepSeek Web2API watchdog autostart was removed; API processes were left unchanged.'
    exit 0
}

if (-not (Test-Path -LiteralPath $WatchScript -PathType Leaf)) {
    throw 'watch-deepseek-dual-tab.ps1 was not found.'
}
$null = [scriptblock]::Create((Get-Content -Raw -LiteralPath $WatchScript))
New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null

if ($WatchScript.Contains('"')) { throw 'Paths containing a double quote are not supported.' }
$runCommand = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $WatchScript + '"'
if (-not (Test-Path -Path $RunKey)) {
    New-Item -Path $RunKey -Force | Out-Null
}
Set-ItemProperty -Path $RunKey -Name $RunName -Value $runCommand

$running = $false
if (Test-Path -LiteralPath $WatchdogStateFile) {
    try {
        $state = Get-Content -Raw -LiteralPath $WatchdogStateFile | ConvertFrom-Json
        $process = Get-CimInstance Win32_Process `
            -Filter "ProcessId = $([int]$state.pid)" -ErrorAction SilentlyContinue
        $running = $process -and $process.CommandLine -and $process.CommandLine.Contains($WatchScript)
    } catch {
        $running = $false
    }
}

if (-not $running) {
    $watchArguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $WatchScript + '"'
    Start-Process -FilePath 'powershell.exe' -ArgumentList $watchArguments -WindowStyle Hidden
}

Write-Output 'DeepSeek Web2API watchdog installed for the current Windows user.'
