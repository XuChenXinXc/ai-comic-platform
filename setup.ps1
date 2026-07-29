$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Join-Path $ProjectRoot "backend"
$FrontendRoot = Join-Path $ProjectRoot "frontend"
$PythonPath = Join-Path $BackendRoot ".venv\Scripts\python.exe"

. (Join-Path $ProjectRoot "platform-process.ps1")

if (-not (Test-Path -LiteralPath $PythonPath)) {
    Invoke-NativeChecked `
        -FilePath "py" `
        -ArgumentList @("-3.11", "-m", "venv", (Join-Path $BackendRoot ".venv")) `
        -FailureMessage "创建 Python 虚拟环境失败"
}

Push-Location $BackendRoot
try {
    Invoke-NativeChecked `
        -FilePath $PythonPath `
        -ArgumentList @("-m", "pip", "install", "--upgrade", "pip") `
        -FailureMessage "升级 pip 失败"
    Invoke-NativeChecked `
        -FilePath $PythonPath `
        -ArgumentList @("-m", "pip", "install", "-e", ".[dev]") `
        -FailureMessage "安装后端依赖失败"
}
finally {
    Pop-Location
}

Push-Location $FrontendRoot
try {
    Invoke-NativeChecked `
        -FilePath "npm.cmd" `
        -ArgumentList @("install") `
        -FailureMessage "安装前端依赖失败"
    Invoke-NativeChecked `
        -FilePath "npx.cmd" `
        -ArgumentList @("playwright", "install", "chromium") `
        -FailureMessage "安装 Playwright Chromium 失败"
}
finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".env"))) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env.example") -Destination (Join-Path $ProjectRoot ".env")
}

Write-Host "环境安装完成。双击 一键启动.cmd 或运行 .\restart.ps1 启动平台。" -ForegroundColor Green
