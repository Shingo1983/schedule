@echo off
chcp 65001 >nul 2>&1
title JAL LSP ショッピング比較ツール

echo ============================================================
echo   JAL LSP ショッピング比較ツール - 起動中...
echo ============================================================
echo.

:: Pythonの存在確認
python --version >nul 2>&1
if errorlevel 1 (
    echo [エラー] Pythonが見つかりません。
    echo https://www.python.org/downloads/ からインストールしてください。
    echo.
    pause
    exit /b 1
)

:: 依存パッケージの自動インストール
echo 依存パッケージを確認中...
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo Flask をインストール中...
    pip install requests flask
    echo.
)

:: Webサーバー起動
echo.
echo ============================================================
echo   ブラウザで http://localhost:5000 を開いてください
echo   終了するにはこのウィンドウを閉じるか Ctrl+C を押してください
echo ============================================================
echo.

:: ブラウザを自動で開く（1秒後）
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"

:: Flaskアプリ起動
python -m jal_shopping_tool web

pause
