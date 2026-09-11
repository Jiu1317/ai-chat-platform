$ErrorActionPreference = 'Stop'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Web2ApiRoot = Join-Path $WorkRoot 'web2api'
$VenvPython = Join-Path $WorkRoot '.venv\Scripts\python.exe'
$ExampleConfig = Join-Path $WorkRoot 'web2api-config.example.json'
$ConfigFile = Join-Path $WorkRoot 'web2api-config.json'

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python 3.11 or newer is required. Install Python and select "Add Python to PATH".'
}
python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) {
    throw 'Python on PATH is older than 3.11. Install Python 3.11 or newer before setup.'
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    python -m venv (Join-Path $WorkRoot '.venv')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        throw 'The Python virtual environment could not be created.'
    }
}

& $VenvPython -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) {
    throw 'The existing .venv uses Python older than 3.11. Recreate it with Python 3.11 or newer.'
}

& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip could not be upgraded.' }
& $VenvPython -m pip install -e $Web2ApiRoot
if ($LASTEXITCODE -ne 0) { throw 'Web2API dependencies could not be installed.' }
& $VenvPython -c 'import aiohttp, chatgpt_web2api'
if ($LASTEXITCODE -ne 0) { throw 'The installed Web2API package could not be imported.' }

if (-not (Test-Path -LiteralPath $ConfigFile)) {
    Copy-Item -LiteralPath $ExampleConfig -Destination $ConfigFile
    Write-Output 'Created web2api-config.json. Replace CHANGE_ME_TO_A_LONG_RANDOM_VALUE before starting.'
}

Write-Output 'Windows bridge setup complete.'
