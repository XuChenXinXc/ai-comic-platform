@echo off
setlocal EnableDelayedExpansion

echo ============================================
echo   AI Comic Platform - Build, Deploy and Launch
echo ============================================
echo.

where pwsh.exe >nul 2>&1
if errorlevel 1 goto use_windows_powershell

echo [1/3] Building with PowerShell 7...
pwsh.exe -NoLogo -NoProfile -File "%~dp0build.ps1"
if errorlevel 1 goto build_failed
echo.
echo [2/3] Starting platform with PowerShell 7...
pwsh.exe -NoLogo -NoProfile -File "%~dp0restart.ps1" %*
goto startup_finished

:use_windows_powershell
echo PowerShell 7 not found, falling back to Windows PowerShell 5.1...
echo [1/3] Building with Windows PowerShell 5.1...
powershell.exe -NoLogo -NoProfile -File "%~dp0build.ps1"
if errorlevel 1 goto build_failed
echo.
echo [2/3] Starting platform with Windows PowerShell 5.1...
powershell.exe -NoLogo -NoProfile -File "%~dp0restart.ps1" %*
goto startup_finished

:build_failed
set "BUILD_EXIT_CODE=%ERRORLEVEL%"
echo.
echo [ERROR] Build failed with exit code %BUILD_EXIT_CODE%.
echo Fix the errors shown above, then run this script again.
if not "!AI_COMIC_NO_PAUSE!"=="1" pause
exit /b %BUILD_EXIT_CODE%

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
    echo [3/3] Platform built and started. See the green URL above for access.
    echo To stop the platform, run .\stop.ps1
    if not "!AI_COMIC_NO_PAUSE!"=="1" (
        echo.
        echo Press any key to close this window - platform keeps running in background...
        pause >nul
    )
)

exit /b %PLATFORM_EXIT_CODE%
