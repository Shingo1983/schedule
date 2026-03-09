@echo off
chcp 65001 >nul 2>&1
title JAL LSP ショッピング比較ツール

:: このbatファイルがあるフォルダに移動
cd /d "%~dp0"

echo.
echo   ====================================================
echo    JAL LSP ショッピング比較ツール
echo   ====================================================
echo.

:: Pythonの存在確認
python --version >nul 2>&1
if errorlevel 1 (
    echo   [エラー] Pythonがインストールされていません。
    echo.
    echo   以下のページからPythonをインストールしてください:
    echo   https://www.python.org/downloads/
    echo.
    echo   インストール時に「Add Python to PATH」に
    echo   チェックを入れるのを忘れないでください!
    echo.
    echo   インストール後、このファイルをもう一度ダブルクリックしてください。
    echo.
    pause
    exit /b 1
)

:: 依存パッケージの自動インストール（初回のみ）
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo   初回セットアップ中です。少々お待ちください...
    echo.
    pip install requests flask >nul 2>&1
    if errorlevel 1 (
        echo   [エラー] パッケージのインストールに失敗しました。
        echo   インターネット接続を確認してください。
        echo.
        pause
        exit /b 1
    )
    echo   セットアップ完了!
    echo.
)

echo   起動中... ブラウザが自動で開きます。
echo.
echo   ----------------------------------------------------
echo   終了するには、このウィンドウを閉じてください。
echo   ----------------------------------------------------
echo.

:: ブラウザを自動で開く（2秒後）
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"

:: Flaskアプリ起動
python -m jal_shopping_tool web

pause
