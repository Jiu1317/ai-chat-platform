param(
    [string]$PythonCommand = '',
    [switch]$SkipDevDependencies
)

$ErrorActionPreference = 'Stop'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VirtualEnvironment = Join-Path $WorkRoot '.venv'
$VirtualPython = Join-Path $VirtualEnvironment 'Scripts\python.exe'
$ExampleConfig = Join-Path $WorkRoot 'config.example.json'
$LocalConfig = Join-Path $WorkRoot 'config.json'

if (-not (Test-Path -LiteralPath $ExampleConfig -PathType Leaf)) {
    throw 'config.example.json was not found.'
}

if (-not (Test-Path -LiteralPath $VirtualPython -PathType Leaf)) {
    if ($PythonCommand) {
        & $PythonCommand -m venv $VirtualEnvironment
    } elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
        & py.exe -3 -m venv $VirtualEnvironment
    } elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
        & python.exe -m venv $VirtualEnvironment
    } else {
        throw 'Python 3.11 or newer was not found. Install Python and run this script again.'
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VirtualPython)) {
        throw 'The Python virtual environment could not be created.'
    }
}

& $VirtualPython -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) {
    throw 'DeepSeek Web2API requires Python 3.11 or newer.'
}

& $VirtualPython -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip could not be upgraded.' }

$packageTarget = if ($SkipDevDependencies) { $WorkRoot } else { "${WorkRoot}[dev]" }
& $VirtualPython -m pip install --disable-pip-version-check --editable $packageTarget
if ($LASTEXITCODE -ne 0) { throw 'DeepSeek Web2API dependencies could not be installed.' }

& $VirtualPython -c 'import aiohttp, deepseek_web2api'
if ($LASTEXITCODE -ne 0) { throw 'The installed DeepSeek Web2API package could not be imported.' }

if (-not (Test-Path -LiteralPath $LocalConfig)) {
    Copy-Item -LiteralPath $ExampleConfig -Destination $LocalConfig
    Write-Output 'Created config.json from the safe example. Replace its API-key placeholder.'
} else {
    Write-Output 'Existing config.json was preserved.'
}

Write-Output 'DeepSeek Web2API environment is ready. Edit config.json, then run start-deepseek-dual-tab.ps1.'
