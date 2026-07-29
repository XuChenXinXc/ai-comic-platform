$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Join-Path $ProjectRoot "backend"
$FrontendRoot = Join-Path $ProjectRoot "frontend"
$PythonPath = Join-Path $BackendRoot ".venv\Scripts\python.exe"

. (Join-Path $ProjectRoot "platform-process.ps1")

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "尚未安装环境，请先运行 .\setup.ps1"
}

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

Push-Location $FrontendRoot
try {
    Invoke-NativeChecked `
        -FilePath "npm.cmd" `
        -ArgumentList @("run", "build") `
        -FailureMessage "前端构建失败"
}
finally {
    Pop-Location
}

Write-Host "构建完成。双击 一键启动.cmd 或运行 .\restart.ps1 启动平台。" -ForegroundColor Green
