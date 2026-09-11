$ErrorActionPreference = 'Stop'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$TrayScript = Join-Path $WorkRoot 'dual-tab-tray.ps1'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunName = 'Web2ApiChromeTray'

if (-not (Test-Path -LiteralPath $TrayScript)) {
    throw 'dual-tab-tray.ps1 was not found.'
}

$null = [scriptblock]::Create((Get-Content -Raw -LiteralPath $TrayScript))
$runCommand = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $TrayScript + '"'

New-Item -Path $RunKey -Force | Out-Null
Set-ItemProperty -Path $RunKey -Name $RunName -Value $runCommand

$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and
    $_.CommandLine -like "*$TrayScript*" -and
    $_.ProcessId -ne $PID
}

if (-not $existing) {
    Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-WindowStyle', 'Hidden',
        '-ExecutionPolicy', 'Bypass', '-File', $TrayScript
    ) -WindowStyle Hidden
}

Write-Output 'Background watchdog and tray controller installed.'
