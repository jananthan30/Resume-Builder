# Kill any existing process on port 8100
$conn = Get-NetTCPConnection -LocalPort 8100 -ErrorAction SilentlyContinue
if ($conn) {
    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Write-Host "Killed existing server"
}

# Read API key from .env in same directory as this script
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $scriptDir ".env"
$apiKey = (Get-Content $envFile | Where-Object { $_ -match '^ANTHROPIC_API_KEY=' }) -replace '^ANTHROPIC_API_KEY=', ''

# Start new server process with API key in environment
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "python"
$psi.Arguments = "scorer_server.py --port 8100"
$psi.WorkingDirectory = $scriptDir
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.EnvironmentVariables["ANTHROPIC_API_KEY"] = $apiKey

$p = [System.Diagnostics.Process]::Start($psi)
Write-Host "Started scorer server with PID: $($p.Id)"
Write-Host "API key set: $($apiKey.Substring(0,20))..."
