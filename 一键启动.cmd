@echo off
setlocal

where pwsh.exe >nul 2>&1
if errorlevel 1 goto use_windows_powershell

pwsh.exe -NoLogo -NoProfile -File "%~dp0restart.ps1" %*
goto startup_finished

:use_windows_powershell
powershell.exe -NoLogo -NoProfile -File "%~dp0restart.ps1" %*

:startup_finished
set "PLATFORM_EXIT_CODE=%ERRORLEVEL%"

if not "%PLATFORM_EXIT_CODE%"=="0" (
    echo.
    echo Platform startup failed with exit code %PLATFORM_EXIT_CODE%.
    if not "%AI_COMIC_NO_PAUSE%"=="1" pause
)

exit /b %PLATFORM_EXIT_CODE%
