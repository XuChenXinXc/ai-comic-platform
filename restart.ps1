[CmdletBinding()]
param(
    [switch]$NoBrowser,

    [ValidateRange(1, 120)]
    [int]$HealthTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Join-Path $ProjectRoot "backend"
$PythonPath = Join-Path $BackendRoot ".venv\Scripts\python.exe"
$FrontendIndex = Join-Path $ProjectRoot "frontend\dist\index.html"
$RunRoot = Join-Path $ProjectRoot ".run"
$StatePath = Join-Path $RunRoot "platform.state.json"
$StandardOutputPath = Join-Path $RunRoot "platform.stdout.log"
$StandardErrorPath = Join-Path $RunRoot "platform.stderr.log"

. (Join-Path $ProjectRoot "platform-process.ps1")

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "尚未安装环境，请先运行 .\setup.ps1"
}

$Mutex = $null
try {
    $Mutex = Enter-PlatformMutex -ProjectRoot $ProjectRoot
    Stop-AllPlatformProcesses `
        -ProjectRoot $ProjectRoot `
        -PythonPath $PythonPath

    if (-not (Test-Path -LiteralPath $FrontendIndex -PathType Leaf)) {
        throw "尚未构建前端，请先运行 .\build.ps1"
    }
    Assert-ExternalCommand `
        -CommandName "ffmpeg" `
        -InstallHint "请安装 FFmpeg，并将 ffmpeg 与 ffprobe 加入 PATH。"
    Assert-ExternalCommand `
        -CommandName "ffprobe" `
        -InstallHint "请安装 FFmpeg，并将 ffmpeg 与 ffprobe 加入 PATH。"

    $RuntimeConfig = Get-PlatformRuntimeConfig `
        -BackendRoot $BackendRoot `
        -PythonPath $PythonPath
    $ClientHost = Get-PlatformClientHost -HostName $RuntimeConfig.Host
    $AppUrl = "http://${ClientHost}:$($RuntimeConfig.Port)"
    $HealthUrl = "$AppUrl/api/health"
    Assert-PortAvailable -Port $RuntimeConfig.Port
    Push-Location $BackendRoot
    try {
        Invoke-NativeChecked `
            -FilePath $PythonPath `
            -ArgumentList @("-m", "alembic", "upgrade", "head") `
            -FailureMessage "数据库迁移失败"
    }
    finally {
        Pop-Location
    }

    $PreviousServeFrontend = [Environment]::GetEnvironmentVariable(
        "APP_SERVE_FRONTEND",
        "Process"
    )
    try {
        $env:APP_SERVE_FRONTEND = "true"
        $State = Start-TrackedProcess `
            -ProjectRoot $ProjectRoot `
            -FilePath $PythonPath `
            -ArgumentList @(
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                $RuntimeConfig.Host,
                "--port",
                [string]$RuntimeConfig.Port
            ) `
            -WorkingDirectory $BackendRoot `
            -StatePath $StatePath `
            -Mode "production" `
            -HostName $RuntimeConfig.Host `
            -Port $RuntimeConfig.Port `
            -StandardOutputPath $StandardOutputPath `
            -StandardErrorPath $StandardErrorPath
    }
    finally {
        if ($null -eq $PreviousServeFrontend) {
            Remove-Item Env:APP_SERVE_FRONTEND -ErrorAction SilentlyContinue
        }
        else {
            $env:APP_SERVE_FRONTEND = $PreviousServeFrontend
        }
    }

    try {
        Wait-PlatformEndpoint `
            -State $State `
            -Url $HealthUrl `
            -TimeoutSeconds $HealthTimeoutSeconds `
            -RequireHealthPayload
    }
    catch {
        $StartupError = $_.Exception.Message
        try {
            Stop-TrackedState `
                -StatePath $StatePath `
                -ProjectRoot $ProjectRoot `
                -ExpectedMode "production" `
                -PythonPath $PythonPath
        }
        catch {
            $StartupError += "`n回滚失败：$($_.Exception.Message)"
        }

        $ErrorTail = Get-LogTail -LogPath $StandardErrorPath
        if (-not [string]::IsNullOrWhiteSpace($ErrorTail)) {
            $StartupError += "`n错误日志：`n$ErrorTail"
        }
        throw $StartupError
    }

    Write-Host "AI 漫剧平台已启动：$AppUrl" -ForegroundColor Green
    Write-Host "PID：$($State.pid)" -ForegroundColor DarkGray
    Write-Host "日志：$StandardOutputPath / $StandardErrorPath" -ForegroundColor DarkGray
    Write-Host "运行 .\stop.ps1 可停止平台。" -ForegroundColor DarkGray

    if (-not $NoBrowser) {
        try {
            Start-Process -FilePath $AppUrl -ErrorAction Stop
        }
        catch {
            Write-Warning "平台已健康启动，但浏览器打开失败：$($_.Exception.Message)"
        }
    }
}
finally {
    Exit-PlatformMutex -Mutex $Mutex
}
