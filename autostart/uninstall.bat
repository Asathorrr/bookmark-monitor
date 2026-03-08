@echo off
chcp 65001 >nul 2>&1
title BookmarkMonitor - Uninstall

echo.
echo  Removing BookmarkMonitor scheduled task...
echo.

schtasks /end /tn "BookmarkMonitor" >nul 2>&1
schtasks /delete /tn "BookmarkMonitor" /f
if errorlevel 1 (
    echo  [INFO] Task not found or already removed.
) else (
    echo  [OK] Task removed successfully.
)

echo.
pause
