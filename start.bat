@echo off
cd /d C:\Users\a4s5d\cb-tracker
echo ============================================
echo  可轉債折溢價追蹤器
echo ============================================
echo.

:: Use local venv if it exists
if exist ".venv\Scripts\uvicorn.exe" (
    echo [START] Launching server at http://127.0.0.1:8000
    echo [INFO]  Press Ctrl+C to stop
    echo.
    start "" "http://127.0.0.1:8000"
    .venv\Scripts\uvicorn.exe backend.main:app --host 127.0.0.1 --port 8000 --reload
    pause
    exit /b 0
)

:: Fallback: try system Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] No Python found. Run setup first:
    echo   uv venv .venv --python 3.12
    echo   uv pip install -r backend\requirements.txt
    pause
    exit /b 1
)

echo [START] Launching server at http://127.0.0.1:8000
echo [INFO]  Press Ctrl+C to stop
echo.
start "" "http://127.0.0.1:8000"
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
pause
