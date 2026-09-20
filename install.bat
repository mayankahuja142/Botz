@echo off
title SafeIM Lead Monitor V2 - One-Click Setup
color 0B
echo.
echo  ====================================================
echo   SafeIM Lead Monitor V2  --  One-Click Setup
echo  ====================================================
echo.

REM Check Python is installed
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    color 0C
    echo  [ERROR] Python not found!
    echo  Please install Python 3.10+ from https://python.org
    echo  and make sure to check "Add to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo  Step 1 of 2: Installing Python packages...
echo  (this may take 1-2 minutes on first run)
echo.
pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    color 0C
    echo.
    echo  [ERROR] pip install failed!
    echo  Try running this script as Administrator, or check your internet connection.
    echo.
    pause
    exit /b 1
)

echo.
echo  Step 2 of 2: Downloading Playwright Chromium browser...
echo  (one-time download ~150 MB)
echo.
playwright install chromium
if %ERRORLEVEL% NEQ 0 (
    color 0C
    echo.
    echo  [ERROR] Playwright browser install failed!
    echo  Try running: python -m playwright install chromium
    echo.
    pause
    exit /b 1
)

echo.
color 0A
echo  ====================================================
echo   Setup complete! You are ready to go.
echo.
echo   To launch the app, run:
echo     python main.py
echo.
echo   On first launch, log in to IndiaMART in the browser
echo   window that opens. Your session will be saved for
echo   future runs.
echo  ====================================================
echo.
pause
