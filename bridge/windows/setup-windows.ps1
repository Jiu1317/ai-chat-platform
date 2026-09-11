$ErrorActionPreference = 'Stop'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $WorkRoot '..\..')).Path
$Web2ApiRoot = Join-Path $ProjectRoot 'services\web2api'
$VenvPython = Join-Path $WorkRoot '.venv\Scripts\python.exe'
$ExampleConfig = Join-Path $WorkRoot 'web2api-config.example.json'
$ConfigFile = Join-Path $WorkRoot 'web2api-config.json'

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python 3.11 or newer is required. Install Python and select "Add Python to PATH".'
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    python -m venv (Join-Path $WorkRoot '.venv')
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e $Web2ApiRoot

if (-not (Test-Path -LiteralPath $ConfigFile)) {
    Copy-Item -LiteralPath $ExampleConfig -Destination $ConfigFile
    Write-Output 'Created web2api-config.json. Replace CHANGE_ME_TO_A_LONG_RANDOM_VALUE before starting.'
}

Write-Output 'Windows bridge setup complete.'
