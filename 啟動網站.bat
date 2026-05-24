@echo off
chcp 65001 >nul
title CB追蹤器

echo ================================
echo   CB 可轉債追蹤器 啟動中...
echo ================================
echo.

cd /d "%~dp0"

:: 啟動後端伺服器（背景執行）
echo [1/2] 啟動伺服器...
start "CB-Server" /min .venv\Scripts\uvicorn.exe backend.main:app --host 127.0.0.1 --port 8000

:: 等待伺服器啟動
timeout /t 3 /nobreak >nul

:: 啟動 Cloudflare Tunnel（背景執行）
echo [2/2] 建立 Cloudflare Tunnel...
set CF_PATH=%LOCALAPPDATA%\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe
start "CF-Tunnel" /min "%CF_PATH%" tunnel --url http://127.0.0.1:8000 --logfile "%~dp0tunnel.log"

:: 等待 Tunnel 取得網址
echo 等待 Tunnel 連線...
timeout /t 6 /nobreak >nul

:: 從 log 找出網址並顯示
for /f "tokens=*" %%a in ('findstr "trycloudflare.com" "%~dp0tunnel.log" 2^>nul') do set LASTLINE=%%a
for /f "tokens=2 delims=|" %%b in ('findstr "trycloudflare.com" "%~dp0tunnel.log" 2^>nul') do (
    set URL=%%b
)

echo.
echo ================================
echo   網站已啟動！
echo.
powershell -Command "Get-Content '%~dp0tunnel.log' | Select-String 'trycloudflare.com' | Select-Object -Last 1" 2>nul
echo.
echo   本機網址: http://127.0.0.1:8000
echo   每日 14:35 自動更新報價
echo ================================
echo.
echo 關閉此視窗會停止網站，請保持開啟。
echo.
pause
