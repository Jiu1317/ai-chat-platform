param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$TrayScript = Join-Path $WorkRoot 'dual-tab-tray.ps1'
$WatchScript = Join-Path $WorkRoot 'watch-dual-tab.ps1'
$ExitSignalFile = Join-Path $WorkRoot '.dual-tab-tray-exit'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunName = 'Web2ApiChromeTray'

function Get-ExistingTrayProcesses {
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.CommandLine -and
            $_.CommandLine.Contains($TrayScript) -and
            $_.ProcessId -ne $PID
        }
    )
}

function Get-ExistingWatchdogProcesses {
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.CommandLine -and
            $_.CommandLine.Contains($WatchScript) -and
            $_.ProcessId -ne $PID
        }
    )
}

function ConvertTo-NativeArgument {
    param([string]$Value)

    if ($Value.Contains('"')) { throw 'Paths containing a double quote are not supported.' }
    if ($Value -match '\s') { return '"' + $Value + '"' }
    return $Value
}

if ($Uninstall) {
    Remove-ItemProperty -Path $RunKey -Name $RunName -ErrorAction SilentlyContinue
    Set-Content -LiteralPath $ExitSignalFile -Value 'exit' -Encoding ASCII

    $deadline = (Get-Date).AddSeconds(8)
    do {
        Start-Sleep -Milliseconds 250
    } while ((Get-ExistingTrayProcesses) -and (Get-Date) -lt $deadline)

    if (Get-ExistingTrayProcesses) {
        Write-Warning 'The tray controller is still closing. Its browser window will be restored when it exits.'
    }
    foreach ($watchdog in @(Get-ExistingWatchdogProcesses)) {
        Stop-Process -Id $watchdog.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Write-Output 'ChatGPT tray autostart and its watchdog were removed; API and browser processes were left unchanged.'
    exit 0
}

if (-not (Test-Path -LiteralPath $TrayScript)) {
    throw 'dual-tab-tray.ps1 was not found.'
}

$null = [scriptblock]::Create((Get-Content -Raw -LiteralPath $TrayScript))
$runCommand = 'powershell.exe -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $TrayScript + '"'

# Clear an old uninstall signal only as part of an explicit installation. If an
# uninstall raced with tray startup, leaving the signal in place lets that late
# process observe it and exit instead of recreating an orphaned tray instance.
Remove-Item -LiteralPath $ExitSignalFile -Force -ErrorAction SilentlyContinue

if (-not (Test-Path -Path $RunKey)) {
    New-Item -Path $RunKey -Force | Out-Null
}
Set-ItemProperty -Path $RunKey -Name $RunName -Value $runCommand

if (-not (Get-ExistingTrayProcesses)) {
    Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-STA', '-WindowStyle', 'Hidden',
        '-ExecutionPolicy', 'Bypass', '-File', (ConvertTo-NativeArgument $TrayScript)
    ) -WindowStyle Hidden
}

Write-Output 'Background watchdog and tray controller installed.'
