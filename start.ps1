[CmdletBinding()]
param(
    [switch]$NoBrowser,

    [ValidateRange(1, 120)]
    [int]$HealthTimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $ProjectRoot "restart.ps1") `
    -NoBrowser:$NoBrowser `
    -HealthTimeoutSeconds $HealthTimeoutSeconds
