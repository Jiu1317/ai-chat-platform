$ErrorActionPreference = 'Stop'

$WorkRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $WorkRoot 'cloudflared-config.yml'

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    throw 'cloudflared is not installed or is not available in PATH.'
}
if (-not (Test-Path -LiteralPath $ConfigFile)) {
    throw 'Copy cloudflared-config.example.yml to cloudflared-config.yml and fill in your tunnel values.'
}

cloudflared tunnel --config $ConfigFile run
