$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Join-Path $ProjectRoot "backend"
$FrontendRoot = Join-Path $ProjectRoot "frontend"
$PythonPath = Join-Path $BackendRoot ".venv\Scripts\python.exe"

. (Join-Path $ProjectRoot "platform-process.ps1")

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "尚未安装环境，请先运行 .\setup.ps1"
}

Push-Location $BackendRoot
try {
    Invoke-NativeChecked `
        -FilePath $PythonPath `
        -ArgumentList @("-m", "ruff", "check", ".") `
        -FailureMessage "后端 ruff 检查失败"
    Invoke-NativeChecked `
        -FilePath $PythonPath `
        -ArgumentList @("-m", "pytest", "-q") `
        -FailureMessage "后端测试失败"
}
finally {
    Pop-Location
}

Push-Location $FrontendRoot
try {
    Invoke-NativeChecked `
        -FilePath "npm.cmd" `
        -ArgumentList @("run", "lint") `
        -FailureMessage "前端 lint 检查失败"
    Invoke-NativeChecked `
        -FilePath "npm.cmd" `
        -ArgumentList @("run", "test", "--", "--run") `
        -FailureMessage "前端测试失败"
    Invoke-NativeChecked `
        -FilePath "npm.cmd" `
        -ArgumentList @("run", "build") `
        -FailureMessage "前端构建失败"
}
finally {
    Pop-Location
}
