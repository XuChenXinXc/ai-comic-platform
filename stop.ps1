[CmdletBinding()]
param(
    [ValidateRange(1, 60)]
    [int]$TimeoutSeconds = 10
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonPath = Join-Path $ProjectRoot "backend\.venv\Scripts\python.exe"

. (Join-Path $ProjectRoot "platform-process.ps1")

$Mutex = Enter-PlatformMutex -ProjectRoot $ProjectRoot
try {
    Stop-AllPlatformProcesses `
        -ProjectRoot $ProjectRoot `
        -PythonPath $PythonPath `
        -TimeoutSeconds $TimeoutSeconds
    Write-Host "本项目已没有受控运行进程。" -ForegroundColor Green
}
finally {
    Exit-PlatformMutex -Mutex $Mutex
}
