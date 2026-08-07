@echo off
setlocal EnableDelayedExpansion

echo ============================================
echo   AI Comic Platform - One-Click Launch
echo ============================================
echo.

where pwsh.exe >nul 2>&1
if errorlevel 1 goto use_windows_powershell

echo [1/2] Starting platform with PowerShell 7...
pwsh.exe -NoLogo -NoProfile -File "%~dp0restart.ps1" %*
goto startup_finished

:use_windows_powershell
echo [1/2] PowerShell 7 not found, falling back to Windows PowerShell 5.1...
powershell.exe -NoLogo -NoProfile -File "%~dp0restart.ps1" %*

:startup_finished
set "PLATFORM_EXIT_CODE=%ERRORLEVEL%"

if not "%PLATFORM_EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Platform startup failed with exit code %PLATFORM_EXIT_CODE%.
    echo Check the log file: .run\platform.stderr.log
    echo If the port is occupied, edit APP_PORT in .env and retry.
    if not "!AI_COMIC_NO_PAUSE!"=="1" pause
) else (
    echo.
    echo [2/2] Platform started. See the green URL above for access.
    echo To stop the platform, run .\stop.ps1
    if not "!AI_COMIC_NO_PAUSE!"=="1" (
        echo.
        echo Press any key to close this window - platform keeps running in background...
        pause >nul
    )
)

exit /b %PLATFORM_EXIT_CODE%
