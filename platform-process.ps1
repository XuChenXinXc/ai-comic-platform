function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$ArgumentList = @(),

        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    & $FilePath @ArgumentList
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "$FailureMessage（退出码 $ExitCode）"
    }
}

function Assert-ExternalCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$CommandName,

        [Parameter(Mandatory = $true)]
        [string]$InstallHint
    )

    $Command = Get-Command `
        -Name $CommandName `
        -CommandType Application `
        -ErrorAction SilentlyContinue
    if ($null -eq $Command) {
        throw "未找到 $CommandName。$InstallHint"
    }
}

function Get-NormalizedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    return [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Enter-PlatformMutex {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,

        [int]$TimeoutSeconds = 30
    )

    $NormalizedRoot = (Get-NormalizedPath -Path $ProjectRoot).ToUpperInvariant()
    $Sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $RootBytes = [System.Text.Encoding]::UTF8.GetBytes($NormalizedRoot)
        $RootHash = [System.BitConverter]::ToString(
            $Sha256.ComputeHash($RootBytes)
        ).Replace("-", "")
    }
    finally {
        $Sha256.Dispose()
    }

    $Mutex = New-Object System.Threading.Mutex(
        $false,
        "Local\AIComicPlatform_$($RootHash.Substring(0, 24))"
    )
    $Acquired = $false
    try {
        try {
            $Acquired = $Mutex.WaitOne($TimeoutSeconds * 1000)
        }
        catch [System.Threading.AbandonedMutexException] {
            $Acquired = $true
        }

        if (-not $Acquired) {
            throw "另一个平台启停操作仍在执行，请稍后重试。"
        }
        return $Mutex
    }
    catch {
        $Mutex.Dispose()
        throw
    }
}

function Exit-PlatformMutex {
    param(
        [System.Threading.Mutex]$Mutex
    )

    if ($null -eq $Mutex) {
        return
    }

    try {
        $Mutex.ReleaseMutex()
    }
    finally {
        $Mutex.Dispose()
    }
}

function Get-PlatformRuntimeConfig {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BackendRoot,

        [Parameter(Mandatory = $true)]
        [string]$PythonPath
    )

    $ConfigCode = @"
import json
import sys

sys.path.insert(0, sys.argv[1])
from app.config import get_settings

settings = get_settings()
print(json.dumps(dict(host=settings.host, port=settings.port)))
"@

    $ConfigJson = & $PythonPath -c $ConfigCode $BackendRoot
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "读取 .env 启动配置失败（退出码 $ExitCode）"
    }

    try {
        $Config = $ConfigJson | ConvertFrom-Json
        $Port = [int]$Config.port
    }
    catch {
        throw "读取 .env 启动配置失败：输出不是有效 JSON。"
    }

    if ([string]::IsNullOrWhiteSpace([string]$Config.host)) {
        throw "APP_HOST 不能为空。"
    }
    if ($Port -lt 1 -or $Port -gt 65535) {
        throw "APP_PORT 必须在 1 到 65535 之间。"
    }

    return [pscustomobject]@{
        Host = [string]$Config.host
        Port = $Port
    }
}

function Get-PlatformClientHost {
    param(
        [Parameter(Mandatory = $true)]
        [string]$HostName
    )

    if ($HostName -in @("0.0.0.0", "::", "[::]")) {
        return "127.0.0.1"
    }
    return $HostName
}

function Get-AllowedPythonExecutables {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonPath
    )

    $Executables = @((Get-NormalizedPath -Path $PythonPath))
    $VenvRoot = Split-Path -Parent (Split-Path -Parent $PythonPath)
    $VenvConfigPath = Join-Path $VenvRoot "pyvenv.cfg"
    if (Test-Path -LiteralPath $VenvConfigPath -PathType Leaf) {
        foreach ($Line in Get-Content -LiteralPath $VenvConfigPath) {
            if ($Line -match "^\s*executable\s*=\s*(.+?)\s*$") {
                $Executables += Get-NormalizedPath -Path $Matches[1]
                break
            }
        }
    }
    return @($Executables | Select-Object -Unique)
}

function Get-ProcessSnapshotFromCim {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Process
    )

    $CreationDate = $Process.CreationDate
    if ($CreationDate -isnot [datetime]) {
        $CreationDate = [System.Management.ManagementDateTimeConverter]::ToDateTime(
            [string]$CreationDate
        )
    }

    return [pscustomobject]@{
        ProcessId = [int]$Process.ProcessId
        ParentProcessId = [int]$Process.ParentProcessId
        StartTimeUtc = $CreationDate.ToUniversalTime()
        Executable = [string]$Process.ExecutablePath
        Command = [string]$Process.CommandLine
    }
}

function Get-ProcessSnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    $Process = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $ProcessId" `
        -ErrorAction SilentlyContinue
    if ($null -eq $Process) {
        return $null
    }
    return Get-ProcessSnapshotFromCim -Process $Process
}

function Test-SnapshotsMatch {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Expected,

        [Parameter(Mandatory = $true)]
        [object]$Actual
    )

    if ($Expected.ProcessId -ne $Actual.ProcessId) {
        return $false
    }
    if ([Math]::Abs(
            ($Expected.StartTimeUtc - $Actual.StartTimeUtc).TotalMilliseconds
        ) -gt 1000) {
        return $false
    }
    if ([string]::IsNullOrWhiteSpace([string]$Expected.Executable) -or
        [string]::IsNullOrWhiteSpace([string]$Actual.Executable) -or
        [string]::IsNullOrWhiteSpace([string]$Expected.Command) -or
        [string]::IsNullOrWhiteSpace([string]$Actual.Command)) {
        return $false
    }
    if (-not [string]::Equals(
            (Get-NormalizedPath -Path $Expected.Executable),
            (Get-NormalizedPath -Path $Actual.Executable),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        return $false
    }
    return [string]::Equals(
        [string]$Expected.Command,
        [string]$Actual.Command,
        [System.StringComparison]::Ordinal
    )
}

function New-PlatformState {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,

        [Parameter(Mandatory = $true)]
        [object]$Snapshot,

        [Parameter(Mandatory = $true)]
        [string]$Mode,

        [Parameter(Mandatory = $true)]
        [string]$HostName,

        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    return [ordered]@{
        schema_version = 1
        project_root = Get-NormalizedPath -Path $ProjectRoot
        pid = $Snapshot.ProcessId
        start_time_utc = $Snapshot.StartTimeUtc.ToString("o")
        start_time_utc_ticks = $Snapshot.StartTimeUtc.Ticks
        executable = Get-NormalizedPath -Path $Snapshot.Executable
        command = $Snapshot.Command
        mode = $Mode
        host = $HostName
        port = $Port
    }
}

function Write-PlatformState {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StatePath,

        [Parameter(Mandatory = $true)]
        [object]$State
    )

    $StateRoot = Split-Path -Parent $StatePath
    New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
    $TemporaryPath = Join-Path $StateRoot (
        ".$([System.IO.Path]::GetFileName($StatePath)).$([guid]::NewGuid().ToString('N')).tmp"
    )

    try {
        $State | ConvertTo-Json -Depth 4 |
            Set-Content -LiteralPath $TemporaryPath -Encoding UTF8
        Move-Item -LiteralPath $TemporaryPath -Destination $StatePath -Force
    }
    finally {
        if (Test-Path -LiteralPath $TemporaryPath) {
            Remove-Item -LiteralPath $TemporaryPath -Force
        }
    }
}

function Read-PlatformState {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StatePath,

        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedMode,

        [Parameter(Mandatory = $true)]
        [string]$PythonPath
    )

    if (-not (Test-Path -LiteralPath $StatePath)) {
        return [pscustomobject]@{
            Status = "missing"
            State = $null
            Message = ""
        }
    }

    try {
        $State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        foreach ($RequiredName in @(
                "schema_version",
                "project_root",
                "pid",
                "start_time_utc",
                "start_time_utc_ticks",
                "executable",
                "command",
                "mode",
                "host",
                "port"
            )) {
            if ($null -eq $State.PSObject.Properties[$RequiredName]) {
                throw "缺少字段 $RequiredName"
            }
        }

        if ([int]$State.schema_version -ne 1) {
            throw "不支持的状态版本"
        }
        if (-not [string]::Equals(
                (Get-NormalizedPath -Path ([string]$State.project_root)),
                (Get-NormalizedPath -Path $ProjectRoot),
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            throw "项目根目录不匹配"
        }
        if ([string]$State.mode -ne $ExpectedMode) {
            throw "运行模式不匹配"
        }

        $ProcessId = [int]$State.pid
        $Port = [int]$State.port
        if ($ProcessId -le 0) {
            throw "PID 无效"
        }
        if ($Port -lt 1 -or $Port -gt 65535) {
            throw "端口无效"
        }

        $StartTimeTicks = [long]$State.start_time_utc_ticks
        if ($StartTimeTicks -le 0 -or
            [string]::IsNullOrWhiteSpace([string]$State.start_time_utc)) {
            throw "启动时间无效"
        }
        $StartTime = New-Object datetime(
            $StartTimeTicks,
            [System.DateTimeKind]::Utc
        )
        if ([string]::IsNullOrWhiteSpace([string]$State.executable) -or
            [string]::IsNullOrWhiteSpace([string]$State.command)) {
            throw "进程身份字段为空"
        }

        if ($ExpectedMode -in @("production", "development-backend")) {
            $StateExecutable = Get-NormalizedPath -Path ([string]$State.executable)
            $ExecutableAllowed = $false
            foreach ($AllowedExecutable in @(
                    Get-AllowedPythonExecutables -PythonPath $PythonPath
                )) {
                if ([string]::Equals(
                        $StateExecutable,
                        $AllowedExecutable,
                        [System.StringComparison]::OrdinalIgnoreCase
                    )) {
                    $ExecutableAllowed = $true
                    break
                }
            }
            if (-not $ExecutableAllowed) {
                throw "Python 可执行文件不匹配"
            }
            if ([string]$State.command -notmatch "(?i)\buvicorn\b" -or
                [string]$State.command -notmatch "(?i)\bapp\.main:app\b") {
                throw "后端命令不匹配"
            }
        }
        elseif ($ExpectedMode -eq "development-frontend") {
            if ([string]$State.command -notmatch "(?i)\bnpm(?:\.cmd)?\b" -or
                [string]$State.command -notmatch "(?i)\brun\s+dev\b") {
                throw "前端命令不匹配"
            }
        }
        else {
            throw "未知运行模式"
        }

        return [pscustomobject]@{
            Status = "valid"
            State = [pscustomobject]@{
                schema_version = 1
                project_root = Get-NormalizedPath -Path ([string]$State.project_root)
                pid = $ProcessId
                start_time_utc = $StartTime.ToUniversalTime()
                executable = Get-NormalizedPath -Path ([string]$State.executable)
                command = [string]$State.command
                mode = [string]$State.mode
                host = [string]$State.host
                port = $Port
            }
            Message = ""
        }
    }
    catch {
        return [pscustomobject]@{
            Status = "invalid"
            State = $null
            Message = $_.Exception.Message
        }
    }
}

function Convert-StateToSnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State
    )

    $StartTime = if ($null -ne $State.PSObject.Properties["start_time_utc_ticks"]) {
        New-Object datetime(
            [long]$State.start_time_utc_ticks,
            [System.DateTimeKind]::Utc
        )
    }
    else {
        ([datetime]$State.start_time_utc).ToUniversalTime()
    }

    return [pscustomobject]@{
        ProcessId = [int]$State.pid
        ParentProcessId = 0
        StartTimeUtc = $StartTime
        Executable = [string]$State.executable
        Command = [string]$State.command
    }
}

function Test-LiveProcessMatchesState {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State
    )

    $Actual = Get-ProcessSnapshot -ProcessId ([int]$State.pid)
    if ($null -eq $Actual) {
        return $false
    }
    $Expected = Convert-StateToSnapshot -State $State
    return Test-SnapshotsMatch -Expected $Expected -Actual $Actual
}

function Get-DescendantSnapshots {
    param(
        [Parameter(Mandatory = $true)]
        [object]$RootSnapshot
    )

    $Results = @()
    $Queue = New-Object System.Collections.Queue
    $Queue.Enqueue([pscustomobject]@{
            ProcessId = $RootSnapshot.ProcessId
            Depth = 0
        })
    $Visited = New-Object "System.Collections.Generic.HashSet[int]"
    [void]$Visited.Add([int]$RootSnapshot.ProcessId)

    while ($Queue.Count -gt 0) {
        $Current = $Queue.Dequeue()
        $Children = Get-CimInstance Win32_Process `
            -Filter "ParentProcessId = $($Current.ProcessId)" `
            -ErrorAction SilentlyContinue
        foreach ($Child in $Children) {
            $ChildId = [int]$Child.ProcessId
            if (-not $Visited.Add($ChildId)) {
                continue
            }

            $Snapshot = Get-ProcessSnapshotFromCim -Process $Child
            if ($Snapshot.StartTimeUtc -lt $RootSnapshot.StartTimeUtc.AddSeconds(-1)) {
                continue
            }

            $Depth = [int]$Current.Depth + 1
            $Results += [pscustomobject]@{
                Snapshot = $Snapshot
                Depth = $Depth
            }
            $Queue.Enqueue([pscustomobject]@{
                    ProcessId = $ChildId
                    Depth = $Depth
                })
        }
    }

    return @($Results | Sort-Object Depth -Descending | ForEach-Object {
            $_.Snapshot
        })
}

function Stop-VerifiedSnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Snapshot
    )

    $Actual = Get-ProcessSnapshot -ProcessId $Snapshot.ProcessId
    if ($null -eq $Actual -or
        -not (Test-SnapshotsMatch -Expected $Snapshot -Actual $Actual)) {
        return
    }
    Stop-Process -Id $Snapshot.ProcessId -Force -ErrorAction SilentlyContinue
}

function Wait-SnapshotExited {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Snapshot,

        [int]$TimeoutSeconds = 10
    )

    $Deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $Actual = Get-Process `
            -Id $Snapshot.ProcessId `
            -ErrorAction SilentlyContinue
        if ($null -eq $Actual) {
            return $true
        }
        try {
            $ActualStartTime = $Actual.StartTime.ToUniversalTime()
            if ([Math]::Abs(
                    ($Snapshot.StartTimeUtc - $ActualStartTime).TotalMilliseconds
                ) -gt 1000) {
                return $true
            }
        }
        catch {
            return $true
        }
        Start-Sleep -Milliseconds 200
    } while ([datetime]::UtcNow -lt $Deadline)
    return $false
}

function Stop-TrackedState {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StatePath,

        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedMode,

        [Parameter(Mandatory = $true)]
        [string]$PythonPath,

        [int]$TimeoutSeconds = 10
    )

    $ReadResult = Read-PlatformState `
        -StatePath $StatePath `
        -ProjectRoot $ProjectRoot `
        -ExpectedMode $ExpectedMode `
        -PythonPath $PythonPath

    if ($ReadResult.Status -eq "missing") {
        return
    }
    if ($ReadResult.Status -eq "invalid") {
        Write-Warning (
            "忽略不可信状态文件 {0}：{1}。未终止任何进程。" -f
            $StatePath,
            $ReadResult.Message
        )
        Remove-Item -LiteralPath $StatePath -Force
        return
    }

    $State = $ReadResult.State
    $RootSnapshot = Convert-StateToSnapshot -State $State
    $ActualRoot = Get-ProcessSnapshot -ProcessId $RootSnapshot.ProcessId
    if ($null -eq $ActualRoot) {
        Remove-Item -LiteralPath $StatePath -Force
        return
    }
    if (-not (Test-SnapshotsMatch -Expected $RootSnapshot -Actual $ActualRoot)) {
        Write-Warning (
            "状态 PID {0} 已不属于本项目，未终止该进程。" -f
            $RootSnapshot.ProcessId
        )
        Remove-Item -LiteralPath $StatePath -Force
        return
    }

    $Descendants = @(Get-DescendantSnapshots -RootSnapshot $ActualRoot)
    foreach ($Descendant in $Descendants) {
        Stop-VerifiedSnapshot -Snapshot $Descendant
    }
    Stop-VerifiedSnapshot -Snapshot $ActualRoot

    $AllSnapshots = @($Descendants) + @($ActualRoot)
    foreach ($Snapshot in $AllSnapshots) {
        if (-not (Wait-SnapshotExited `
                -Snapshot $Snapshot `
                -TimeoutSeconds $TimeoutSeconds)) {
            throw "进程 PID $($Snapshot.ProcessId) 未能在超时内停止；状态文件已保留。"
        }
    }

    Remove-Item -LiteralPath $StatePath -Force
    Write-Host "已停止 $ExpectedMode（PID $($ActualRoot.ProcessId)）。"
}

function Remove-LegacyPidFiles {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RunRoot
    )

    foreach ($Name in @("backend.pid", "frontend.pid")) {
        $LegacyPath = Join-Path $RunRoot $Name
        if (-not (Test-Path -LiteralPath $LegacyPath)) {
            continue
        }
        Write-Warning "旧 PID 文件 $LegacyPath 缺少身份信息，已移除记录但未终止任何进程。"
        Remove-Item -LiteralPath $LegacyPath -Force
    }
}

function Stop-AllPlatformProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,

        [Parameter(Mandatory = $true)]
        [string]$PythonPath,

        [int]$TimeoutSeconds = 10
    )

    $RunRoot = Join-Path $ProjectRoot ".run"
    New-Item -ItemType Directory -Path $RunRoot -Force | Out-Null
    foreach ($Definition in @(
            @{ File = "frontend.state.json"; Mode = "development-frontend" },
            @{ File = "backend.state.json"; Mode = "development-backend" },
            @{ File = "platform.state.json"; Mode = "production" }
        )) {
        Stop-TrackedState `
            -StatePath (Join-Path $RunRoot $Definition.File) `
            -ProjectRoot $ProjectRoot `
            -ExpectedMode $Definition.Mode `
            -PythonPath $PythonPath `
            -TimeoutSeconds $TimeoutSeconds
    }
    Remove-LegacyPidFiles -RunRoot $RunRoot
}

function Get-PortListeners {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    return @(
        Get-NetTCPConnection `
            -LocalPort $Port `
            -State Listen `
            -ErrorAction SilentlyContinue
    )
}

function Assert-PortAvailable {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    $Listeners = @(Get-PortListeners -Port $Port)
    if ($Listeners.Count -eq 0) {
        return
    }

    $OwnerDescriptions = @(
        $Listeners |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object {
                $OwnerId = [int]$_
                $Owner = Get-Process -Id $OwnerId -ErrorAction SilentlyContinue
                if ($null -eq $Owner) {
                    "PID $OwnerId"
                }
                else {
                    "PID $OwnerId ($($Owner.ProcessName))"
                }
            }
    )
    throw (
        "端口 {0} 已被非本次启动的进程占用：{1}。为避免误杀，未终止这些进程。`n" +
        "若该进程是本平台上次启动遗留，可先运行 .\stop.ps1 停止后重试；" +
        "否则请先核对占用者（PID/进程名）是否为可信进程，再决定是否直接访问该端口。" -f
        $Port,
        ($OwnerDescriptions -join "、")
    )
}

function Test-ProcessBelongsToRoot {
    param(
        [Parameter(Mandatory = $true)]
        [int]$CandidateProcessId,

        [Parameter(Mandatory = $true)]
        [int]$RootProcessId
    )

    $Visited = New-Object "System.Collections.Generic.HashSet[int]"
    $CurrentId = $CandidateProcessId
    for ($Depth = 0; $Depth -lt 64; $Depth++) {
        if ($CurrentId -eq $RootProcessId) {
            return $true
        }
        if ($CurrentId -le 0 -or -not $Visited.Add($CurrentId)) {
            return $false
        }

        $Current = Get-CimInstance Win32_Process `
            -Filter "ProcessId = $CurrentId" `
            -ErrorAction SilentlyContinue
        if ($null -eq $Current) {
            return $false
        }
        $CurrentId = [int]$Current.ParentProcessId
    }
    return $false
}

function Wait-PlatformEndpoint {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State,

        [Parameter(Mandatory = $true)]
        [string]$Url,

        [int]$TimeoutSeconds = 30,

        [switch]$RequireHealthPayload
    )

    $Deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)
    $LastFailure = "尚未响应"
    do {
        if (-not (Test-LiveProcessMatchesState -State $State)) {
            throw "启动进程已提前退出。"
        }

        try {
            $Response = Invoke-WebRequest `
                -Uri $Url `
                -UseBasicParsing `
                -TimeoutSec 2 `
                -ErrorAction Stop
            $PayloadOk = $true
            $PayloadStatus = ""
            if ($RequireHealthPayload) {
                $Payload = $Response.Content | ConvertFrom-Json
                $PayloadStatus = [string]$Payload.status
                # 实际契约：/api/health 的 status 取值 healthy/degraded/unhealthy。
                # degraded 仅表示 FFmpeg 异常或磁盘预警，服务仍可用，视为已就绪。
                $PayloadOk = $PayloadStatus -in @("ok", "healthy", "degraded")
            }

            $OwnsListener = $false
            foreach ($Listener in @(Get-PortListeners -Port ([int]$State.port))) {
                if (Test-ProcessBelongsToRoot `
                        -CandidateProcessId ([int]$Listener.OwningProcess) `
                        -RootProcessId ([int]$State.pid)) {
                    $OwnsListener = $true
                    break
                }
            }

            if ($Response.StatusCode -eq 200 -and $PayloadOk -and $OwnsListener) {
                return
            }
            $Problems = @()
            if ($RequireHealthPayload -and -not $PayloadOk) {
                $Problems += "health.status='$PayloadStatus'"
            }
            if ($Response.StatusCode -ne 200) {
                $Problems += "HTTP $($Response.StatusCode)"
            }
            if (-not $OwnsListener) {
                $Problems += (
                    "端口 {0} 未被跟踪进程（PID {1}）及其子进程占用" -f
                    $State.port,
                    $State.pid
                )
            }
            $LastFailure = "响应或端口所有权校验未通过（" + ($Problems -join "；") + "）"
        }
        catch {
            $LastFailure = $_.Exception.Message
        }
        Start-Sleep -Milliseconds 250
    } while ([datetime]::UtcNow -lt $Deadline)

    throw "等待服务就绪超时：$LastFailure"
}

function Start-TrackedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,

        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,

        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,

        [Parameter(Mandatory = $true)]
        [string]$StatePath,

        [Parameter(Mandatory = $true)]
        [string]$Mode,

        [Parameter(Mandatory = $true)]
        [string]$HostName,

        [Parameter(Mandatory = $true)]
        [int]$Port,

        [Parameter(Mandatory = $true)]
        [string]$StandardOutputPath,

        [Parameter(Mandatory = $true)]
        [string]$StandardErrorPath
    )

    foreach ($LogPath in @($StandardOutputPath, $StandardErrorPath)) {
        $LogRoot = Split-Path -Parent $LogPath
        New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
        if (Test-Path -LiteralPath $LogPath) {
            Remove-Item -LiteralPath $LogPath -Force
        }
    }

    $StartedProcess = $null
    $LaunchSnapshot = $null
    $StateSnapshot = $null
    try {
        $StartedProcess = Start-Process `
            -FilePath $FilePath `
            -ArgumentList $ArgumentList `
            -WorkingDirectory $WorkingDirectory `
            -WindowStyle Hidden `
            -RedirectStandardOutput $StandardOutputPath `
            -RedirectStandardError $StandardErrorPath `
            -PassThru

        $SnapshotDeadline = [datetime]::UtcNow.AddSeconds(5)
        do {
            $LaunchSnapshot = Get-ProcessSnapshot -ProcessId $StartedProcess.Id
            if ($null -ne $LaunchSnapshot -and
                -not [string]::IsNullOrWhiteSpace($LaunchSnapshot.Executable) -and
                -not [string]::IsNullOrWhiteSpace($LaunchSnapshot.Command)) {
                break
            }
            Start-Sleep -Milliseconds 100
        } while ([datetime]::UtcNow -lt $SnapshotDeadline)

        if ($null -eq $LaunchSnapshot -or
            [string]::IsNullOrWhiteSpace($LaunchSnapshot.Executable) -or
            [string]::IsNullOrWhiteSpace($LaunchSnapshot.Command)) {
            throw "无法读取新进程的可验证身份。"
        }

        $StateSnapshot = $LaunchSnapshot
        if ($Mode -in @("production", "development-backend")) {
            $AllowedExecutables = @(
                Get-AllowedPythonExecutables -PythonPath $FilePath
            )
            $ChildDeadline = [datetime]::UtcNow.AddSeconds(5)
            do {
                $PythonChild = @(
                    Get-DescendantSnapshots -RootSnapshot $LaunchSnapshot |
                        Where-Object {
                            $Child = $_
                            # 快照可能包含刚退出的进程（Executable/Command 为空），
                            # 跳过它们，避免 Get-NormalizedPath 因空路径抛错。
                            if ([string]::IsNullOrWhiteSpace($Child.Executable) -or
                                [string]::IsNullOrWhiteSpace($Child.Command)) {
                                return $false
                            }
                            $Allowed = $false
                            foreach ($AllowedExecutable in $AllowedExecutables) {
                                if ([string]::Equals(
                                        (Get-NormalizedPath -Path $Child.Executable),
                                        $AllowedExecutable,
                                        [System.StringComparison]::OrdinalIgnoreCase
                                    )) {
                                    $Allowed = $true
                                    break
                                }
                            }
                            $Allowed -and
                            $Child.Command -match "(?i)\buvicorn\b" -and
                            $Child.Command -match "(?i)\bapp\.main:app\b"
                        } |
                        Sort-Object StartTimeUtc |
                        Select-Object -First 1
                )
                if ($PythonChild.Count -gt 0) {
                    $StateSnapshot = $PythonChild[0]
                    break
                }
                Start-Sleep -Milliseconds 100
            } while ([datetime]::UtcNow -lt $ChildDeadline)
        }

        $State = New-PlatformState `
            -ProjectRoot $ProjectRoot `
            -Snapshot $StateSnapshot `
            -Mode $Mode `
            -HostName $HostName `
            -Port $Port
        Write-PlatformState -StatePath $StatePath -State $State
        return [pscustomobject]$State
    }
    catch {
        if ($null -ne $LaunchSnapshot) {
            foreach ($Descendant in @(
                    Get-DescendantSnapshots -RootSnapshot $LaunchSnapshot
                )) {
                Stop-VerifiedSnapshot -Snapshot $Descendant
            }
            Stop-VerifiedSnapshot -Snapshot $LaunchSnapshot
        }
        elseif ($null -ne $StartedProcess) {
            Stop-Process -Id $StartedProcess.Id -Force -ErrorAction SilentlyContinue
        }
        throw
    }
}

function Get-LogTail {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LogPath,

        [int]$LineCount = 20
    )

    if (-not (Test-Path -LiteralPath $LogPath)) {
        return ""
    }
    return (Get-Content -LiteralPath $LogPath -Tail $LineCount) -join [Environment]::NewLine
}
