@echo off
cd /d "%~dp0"
title JAL Shopping Tool
echo.
echo ====================================================
echo  JAL LSP Shopping Comparison Tool
echo ====================================================
echo.
python --version >nul 2>&1
if not errorlevel 1 goto :python_ok
echo --------------------------------------------------
echo  Python is not installed!
echo --------------------------------------------------
echo.
echo  Step 1: Open this URL in your browser:
echo.
echo      https://www.python.org/downloads/
echo.
echo  Step 2: Click the yellow "Download Python" button
echo.
echo  Step 3: Run the downloaded file
echo.
echo  Step 4: CHECK "Add python.exe to PATH"
echo          This checkbox is at the BOTTOM of the installer.
echo          YOU MUST CHECK IT or this tool will not work!
echo.
echo  Step 5: Click "Install Now"
echo.
echo  Step 6: After install, double-click start.bat again
echo --------------------------------------------------
echo.
pause
exit /b 1
:python_ok
echo [OK] Python found
echo.
python -c "import flask" >nul 2>&1
if not errorlevel 1 goto :flask_ok
echo Installing required packages (first time only)...
echo This may take 30-60 seconds...
echo.
pip install requests flask
if not errorlevel 1 goto :install_ok
echo.
echo [ERROR] Package install failed.
echo Please check your internet connection and try again.
echo.
pause
exit /b 1
:install_ok
echo.
echo [OK] Setup complete!
echo.
:flask_ok
echo Starting... Your browser will open automatically.
echo.
echo ====================================================
echo To STOP: close this window or press Ctrl+C
echo ====================================================
echo.
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"
python -m jal_shopping_tool web
echo.
echo ====================================================
echo The app has stopped.
echo If this was unexpected, check the error message above.
echo ====================================================
echo.
pause
