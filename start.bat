@echo off
cd /d "%~dp0"
title JAL Shopping Tool

echo.
echo   ====================================================
echo    JAL LSP Shopping Comparison Tool
echo   ====================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Python is not installed.
    echo.
    echo   Please install Python from:
    echo   https://www.python.org/downloads/
    echo.
    echo   IMPORTANT: Check "Add python.exe to PATH" during install!
    echo.
    echo   After installing, double-click this file again.
    echo.
    pause
    exit /b 1
)

echo   Python OK
echo.

:: Install packages (first time only)
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo   Installing required packages (first time only)...
    echo.
    pip install requests flask
    if errorlevel 1 (
        echo.
        echo   [ERROR] Package install failed.
        echo   Please check your internet connection.
        echo.
        pause
        exit /b 1
    )
    echo.
    echo   Setup complete!
    echo.
)

echo   Starting... Your browser will open automatically.
echo.
echo   ----------------------------------------------------
echo   To stop: close this window or press Ctrl+C
echo   ----------------------------------------------------
echo.

:: Open browser after 2 seconds
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"

:: Start Flask app
python -m jal_shopping_tool web

pause
