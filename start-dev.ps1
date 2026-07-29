[CmdletBinding()]
param(
    [switch]$NoBrowser,

    [ValidateRange(1, 120)]
    [int]$HealthTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Join-Path $ProjectRoot "backend"
$FrontendRoot = Join-Path $ProjectRoot "frontend"
$PythonPath = Join-Path $BackendRoot ".venv\Scripts\python.exe"
$RunRoot = Join-Path $ProjectRoot ".run"
$BackendStatePath = Join-Path $RunRoot "backend.state.json"
$FrontendStatePath = Join-Path $RunRoot "frontend.state.json"
$BackendOutputPath = Join-Path $RunRoot "backend.stdout.log"
$BackendErrorPath = Join-Path $RunRoot "backend.stderr.log"
$FrontendOutputPath = Join-Path $RunRoot "frontend.stdout.log"
$FrontendErrorPath = Join-Path $RunRoot "frontend.stderr.log"

. (Join-Path $ProjectRoot "platform-process.ps1")

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "尚未安装环境，请先运行 .\setup.ps1"
}
Assert-ExternalCommand `
    -CommandName "ffmpeg" `
    -InstallHint "请安装 FFmpeg，并将 ffmpeg 与 ffprobe 加入 PATH。"
Assert-ExternalCommand `
    -CommandName "ffprobe" `
    -InstallHint "请安装 FFmpeg，并将 ffmpeg 与 ffprobe 加入 PATH。"

$NpmCommand = Get-Command "npm.cmd" -ErrorAction Stop
$RuntimeConfig = Get-PlatformRuntimeConfig `
    -BackendRoot $BackendRoot `
    -PythonPath $PythonPath

$BackendClientHost = Get-PlatformClientHost -HostName $RuntimeConfig.Host
$BackendUrl = "http://${BackendClientHost}:$($RuntimeConfig.Port)"
$FrontendHost = "127.0.0.1"
$FrontendPort = 3000
$FrontendUrl = "http://${FrontendHost}:$FrontendPort"
$Mutex = Enter-PlatformMutex -ProjectRoot $ProjectRoot

try {
    Stop-AllPlatformProcesses `
        -ProjectRoot $ProjectRoot `
        -PythonPath $PythonPath
    Assert-PortAvailable -Port $RuntimeConfig.Port
    Assert-PortAvailable -Port $FrontendPort

    try {
        $BackendState = Start-TrackedProcess `
            -ProjectRoot $ProjectRoot `
            -FilePath $PythonPath `
            -ArgumentList @(
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                $RuntimeConfig.Host,
                "--port",
                [string]$RuntimeConfig.Port,
                "--reload"
            ) `
            -WorkingDirectory $BackendRoot `
            -StatePath $BackendStatePath `
            -Mode "development-backend" `
            -HostName $RuntimeConfig.Host `
            -Port $RuntimeConfig.Port `
            -StandardOutputPath $BackendOutputPath `
            -StandardErrorPath $BackendErrorPath
        Wait-PlatformEndpoint `
            -State $BackendState `
            -Url "$BackendUrl/api/health" `
            -TimeoutSeconds $HealthTimeoutSeconds `
            -RequireHealthPayload

        $PreviousBackendUrl = [Environment]::GetEnvironmentVariable(
            "VITE_BACKEND_URL",
            "Process"
        )
        try {
            $env:VITE_BACKEND_URL = $BackendUrl
            $FrontendState = Start-TrackedProcess `
                -ProjectRoot $ProjectRoot `
                -FilePath $NpmCommand.Source `
                -ArgumentList @(
                    "run",
                    "dev",
                    "--",
                    "--host",
                    $FrontendHost,
                    "--port",
                    [string]$FrontendPort,
                    "--strictPort"
                ) `
                -WorkingDirectory $FrontendRoot `
                -StatePath $FrontendStatePath `
                -Mode "development-frontend" `
                -HostName $FrontendHost `
                -Port $FrontendPort `
                -StandardOutputPath $FrontendOutputPath `
                -StandardErrorPath $FrontendErrorPath
        }
        finally {
            if ($null -eq $PreviousBackendUrl) {
                Remove-Item Env:VITE_BACKEND_URL -ErrorAction SilentlyContinue
            }
            else {
                $env:VITE_BACKEND_URL = $PreviousBackendUrl
            }
        }
        Wait-PlatformEndpoint `
            -State $FrontendState `
            -Url $FrontendUrl `
            -TimeoutSeconds $HealthTimeoutSeconds
    }
    catch {
        $StartupError = $_.Exception.Message
        try {
            Stop-AllPlatformProcesses `
                -ProjectRoot $ProjectRoot `
                -PythonPath $PythonPath
        }
        catch {
            $StartupError += "`n回滚失败：$($_.Exception.Message)"
        }

        foreach ($LogPath in @($BackendErrorPath, $FrontendErrorPath)) {
            $ErrorTail = Get-LogTail -LogPath $LogPath
            if (-not [string]::IsNullOrWhiteSpace($ErrorTail)) {
                $StartupError += "`n$LogPath：`n$ErrorTail"
            }
        }
        throw $StartupError
    }

    Write-Host "开发环境已启动：$FrontendUrl" -ForegroundColor Green
    Write-Host "后端：$BackendUrl（PID $($BackendState.pid)）" -ForegroundColor DarkGray
    Write-Host "前端：$FrontendUrl（PID $($FrontendState.pid)）" -ForegroundColor DarkGray
    Write-Host "运行 .\stop.ps1 可停止本项目进程。" -ForegroundColor DarkGray

    if (-not $NoBrowser) {
        try {
            Start-Process -FilePath $FrontendUrl -ErrorAction Stop
        }
        catch {
            Write-Warning "开发环境已健康启动，但浏览器打开失败：$($_.Exception.Message)"
        }
    }
}
finally {
    Exit-PlatformMutex -Mutex $Mutex
}
