[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$UseDevWeb,
    [int]$WebPort = 5173,
    [int]$ApiPort = 8000,
    [int]$RuntimePort = 8010
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '.')).Path
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$VenvUvicorn = Join-Path $ProjectRoot '.venv\Scripts\uvicorn.exe'
$LogRoot = Join-Path $ProjectRoot '.logs'
$RuntimeOutLog = Join-Path $LogRoot 'runtime.stdout.log'
$RuntimeErrLog = Join-Path $LogRoot 'runtime.stderr.log'
$ApiOutLog = Join-Path $LogRoot 'api.stdout.log'
$ApiErrLog = Join-Path $LogRoot 'api.stderr.log'
$WorkerOutLog = Join-Path $LogRoot 'worker.stdout.log'
$WorkerErrLog = Join-Path $LogRoot 'worker.stderr.log'
$WebOutLog = Join-Path $LogRoot 'web.stdout.log'
$WebErrLog = Join-Path $LogRoot 'web.stderr.log'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Python virtual environment not found: $VenvPython"
}
if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    throw 'pnpm was not found. Install pnpm or enable Corepack first.'
}

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

function Stop-ProjectProcess {
    param([System.Diagnostics.Process]$Process)

    try {
        if (-not $Process.HasExited) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped PID $($Process.Id)"
        }
    } catch {
        Write-Warning "Failed to stop PID $($Process.Id): $($_.Exception.Message)"
    }
}

Write-Host '=== Stopping CineForge services ===' -ForegroundColor Cyan
Get-CimInstance Win32_Process | Where-Object {
    $commandLine = $_.CommandLine
    $isCurrentShell = $_.ProcessId -eq $PID
    $isProjectService = $commandLine -and (
        $commandLine -match 'app\.main:app' -or
        $commandLine -match 'app\.worker' -or
        $commandLine -match 'runtime\.main:app' -or
        $commandLine -match '(?:^|[\\/])vite(?:\.js)?(?:\s|$)' -or
        $commandLine -match 'pnpm(?:\.cmd)?[^\r\n]*(?:dev:web|@cineforge/web[^\r\n]*(?:dev|preview))'
    )
    (-not $isCurrentShell) -and $isProjectService
} | ForEach-Object {
    $process = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
    if ($process) { Stop-ProjectProcess -Process $process }
}

# Release only the ports owned by this project.
foreach ($port in @($WebPort, $ApiPort, $RuntimePort) | Select-Object -Unique) {
    Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object {
            $process = Get-Process -Id $_ -ErrorAction SilentlyContinue
            if ($process) { Stop-ProjectProcess -Process $process }
        }
}

Start-Sleep -Milliseconds 500

if (-not $SkipBuild) {
    Write-Host '=== Building web app ===' -ForegroundColor Cyan
    Push-Location $ProjectRoot
    try {
        & pnpm --filter @cineforge/web build
        if ($LASTEXITCODE -ne 0) { throw "Web build failed with exit code $LASTEXITCODE" }

        Write-Host '=== Validating Python services ===' -ForegroundColor Cyan
        & $VenvPython -m compileall -q (Join-Path $ProjectRoot 'apps/api/app') (Join-Path $ProjectRoot 'services/agent-runtime/agentscope/runtime')
        if ($LASTEXITCODE -ne 0) { throw "Python validation failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
}

Write-Host '=== Starting Agent Runtime ===' -ForegroundColor Cyan
Start-Process -FilePath $VenvUvicorn -WorkingDirectory (Join-Path $ProjectRoot 'services/agent-runtime/agentscope') -ArgumentList @('runtime.main:app', '--host', '127.0.0.1', '--port', "$RuntimePort") -RedirectStandardOutput $RuntimeOutLog -RedirectStandardError $RuntimeErrLog -WindowStyle Hidden | Out-Null

Write-Host '=== Starting API ===' -ForegroundColor Cyan
Start-Process -FilePath $VenvUvicorn -WorkingDirectory $ProjectRoot -ArgumentList @('app.main:app', '--app-dir', 'apps/api', '--host', '0.0.0.0', '--port', "$ApiPort") -RedirectStandardOutput $ApiOutLog -RedirectStandardError $ApiErrLog -WindowStyle Hidden | Out-Null

Write-Host '=== Starting Worker ===' -ForegroundColor Cyan
Start-Process -FilePath $VenvPython -WorkingDirectory (Join-Path $ProjectRoot 'apps/api') -ArgumentList @('-m', 'app.worker') -RedirectStandardOutput $WorkerOutLog -RedirectStandardError $WorkerErrLog -WindowStyle Hidden | Out-Null

Write-Host '=== Starting Web ===' -ForegroundColor Cyan
if ($UseDevWeb) {
    Start-Process -FilePath 'pnpm.cmd' -WorkingDirectory $ProjectRoot -ArgumentList @('--filter', '@cineforge/web', 'dev', '--host', '0.0.0.0', '--port', "$WebPort") -RedirectStandardOutput $WebOutLog -RedirectStandardError $WebErrLog -WindowStyle Hidden | Out-Null
} else {
    $webDist = Join-Path $ProjectRoot 'apps/web/dist'
    if (-not (Test-Path -LiteralPath $webDist)) { throw "Web build output not found: $webDist" }
    Start-Process -FilePath 'pnpm.cmd' -WorkingDirectory $ProjectRoot -ArgumentList @('--filter', '@cineforge/web', 'preview', '--host', '0.0.0.0', '--port', "$WebPort") -RedirectStandardOutput $WebOutLog -RedirectStandardError $WebErrLog -WindowStyle Hidden | Out-Null
}

Start-Sleep -Seconds 2
Write-Host ''
Write-Host 'CineForge restart completed.' -ForegroundColor Green
Write-Host "Web:     http://127.0.0.1:$WebPort"
Write-Host "API:     http://127.0.0.1:$ApiPort"
Write-Host "Runtime: http://127.0.0.1:$RuntimePort/health"
Write-Host "Logs:    $LogRoot"
