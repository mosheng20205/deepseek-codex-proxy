$ErrorActionPreference = "Stop"

function Get-EnvValue {
    param([Parameter(Mandatory = $true)][string]$Name)

    $value = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ($value) { return $value }

    $value = [Environment]::GetEnvironmentVariable($Name, "User")
    if ($value) { return $value }

    $value = [Environment]::GetEnvironmentVariable($Name, "Machine")
    if ($value) { return $value }

    return ""
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$proxyPath = Join-Path $scriptDir "DeepSeekProxy.py"

$apiKey = Get-EnvValue "DEEPSEEK_API_KEY"
if (-not $apiKey) {
    throw "DEEPSEEK_API_KEY is not set. Set it first, then rerun this script."
}

if (-not (Get-EnvValue "DEEPSEEK_MODEL")) {
    $env:DEEPSEEK_MODEL = "deepseek-v4-pro"
}

if (-not (Get-EnvValue "DEEPSEEK_BASE_URL")) {
    $env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
}

if (-not (Get-EnvValue "DEEPSEEK_THINKING")) {
    $env:DEEPSEEK_THINKING = "disabled"
}

$env:DEEPSEEK_API_KEY = $apiKey

$listener = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $pidText = ($listener | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    throw "Port 127.0.0.1:3000 is already in use by process id(s): $pidText"
}

Write-Host "Starting DeepSeek proxy on http://127.0.0.1:3000"
Write-Host "Model: $($env:DEEPSEEK_MODEL)"
Write-Host "Base URL: $($env:DEEPSEEK_BASE_URL)"
Write-Host "Thinking: $($env:DEEPSEEK_THINKING)"

python $proxyPath
