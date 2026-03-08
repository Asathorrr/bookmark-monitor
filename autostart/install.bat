@echo off
title BookmarkMonitor Install

echo ============================================
echo   BookmarkMonitor Install
echo ============================================
echo.

:: Find project root (parent of autostart\)
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
pushd "%SCRIPT_DIR%\.."
set "PROJECT_DIR=%CD%"
popd
set "APP_PY=%PROJECT_DIR%\app.py"

if not exist "%APP_PY%" (
    echo [ERROR] app.py not found at: %APP_PY%
    echo Make sure install.bat is inside the autostart\ subfolder.
    pause
    exit /b 1
)
echo [OK] Project: %PROJECT_DIR%

:: Find pythonw (no console window)
set "PYTHONW="
where pythonw >nul 2>&1
if not errorlevel 1 for /f "delims=" %%i in ('where pythonw') do set "PYTHONW=%%i"

:: Fall back to python if pythonw not found
if "%PYTHONW%"=="" (
    where python >nul 2>&1
    if not errorlevel 1 for /f "delims=" %%i in ('where python') do set "PYTHONW=%%i"
)

:: Check venv
if exist "%PROJECT_DIR%\venv\Scripts\pythonw.exe" set "PYTHONW=%PROJECT_DIR%\venv\Scripts\pythonw.exe"
if exist "%PROJECT_DIR%\.venv\Scripts\pythonw.exe" set "PYTHONW=%PROJECT_DIR%\.venv\Scripts\pythonw.exe"

if "%PYTHONW%"=="" (
    echo [ERROR] Python not found. Install Python and add to PATH.
    pause
    exit /b 1
)
echo [OK] Python: %PYTHONW%

:: Delete old task if exists
schtasks /delete /tn "BookmarkMonitor" /f >nul 2>&1

:: Create scheduled task - runs at logon, highest privilege
schtasks /create /tn "BookmarkMonitor" /tr "\"%PYTHONW%\" \"%APP_PY%\"" /sc onlogon /ru "%USERNAME%" /rl highest /delay 0000:15 /f

if errorlevel 1 (
    echo [ERROR] Failed to create task. Run as Administrator.
    pause
    exit /b 1
)

echo [OK] Scheduled task created.
echo.

set /p RUN= Start now? [Y/n]: 
if /i "%RUN%" neq "n" (
    schtasks /run /tn "BookmarkMonitor"
    ping 127.0.0.1 -n 4 >nul
    echo [OK] Started. Visit http://localhost:5000
)

echo.
echo ============================================
echo   Done! BookmarkMonitor will auto-start
echo   on next login.
echo   URL: http://localhost:5000
echo   To uninstall: run uninstall.bat
echo ============================================
pause
