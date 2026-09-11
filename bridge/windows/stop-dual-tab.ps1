$ErrorActionPreference = 'Stop'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $WorkRoot 'web2api-config.json'
$GatewayFile = Join-Path $WorkRoot 'dual_tab_gateway.py'
$ProcessStateFile = Join-Path $WorkRoot 'dual-tab-processes.json'

$targets = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and (
        $_.CommandLine -like "*$GatewayFile*" -or
        ($_.CommandLine -match 'chatgpt_web2api' -and $_.CommandLine -like "*$ConfigFile*")
    )
}

foreach ($process in $targets) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $ProcessStateFile -Force -ErrorAction SilentlyContinue

Write-Output "Stopped $($targets.Count) dual-tab process(es)."
