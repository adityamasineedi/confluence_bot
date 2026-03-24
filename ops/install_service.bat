@echo off
REM ============================================================
REM  confluence_bot — Windows Service Installer (NSSM)
REM
REM  Requirements:
REM    1. NSSM installed: https://nssm.cc/download
REM       Place nssm.exe in C:\tools\nssm\ or update NSSM_PATH below
REM    2. Run this script as Administrator
REM
REM  Usage:
REM    install_service.bat          — install + start the service
REM    install_service.bat remove   — stop + remove the service
REM ============================================================

setlocal

set SERVICE_NAME=confluence_bot
set NSSM_PATH=C:\tools\nssm\nssm.exe
set BOT_DIR=C:\projects\confluence_bot
set PYTHON_EXE=%BOT_DIR%\venv\Scripts\python.exe
set LOG_DIR=%BOT_DIR%\logs

REM ── Verify NSSM is present ──────────────────────────────────
if not exist "%NSSM_PATH%" (
    echo [FAIL] NSSM not found at %NSSM_PATH%
    echo        Download from https://nssm.cc/download
    echo        and place nssm.exe at %NSSM_PATH%
    pause
    exit /b 1
)

REM ── Verify Python venv ──────────────────────────────────────
if not exist "%PYTHON_EXE%" (
    echo [FAIL] Python venv not found at %PYTHON_EXE%
    echo        Run:  cd %BOT_DIR% ^& python -m venv venv ^& venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

REM ── Remove mode ─────────────────────────────────────────────
if /i "%1"=="remove" (
    echo Stopping and removing service %SERVICE_NAME%...
    "%NSSM_PATH%" stop   %SERVICE_NAME%
    "%NSSM_PATH%" remove %SERVICE_NAME% confirm
    echo Done.
    pause
    exit /b 0
)

REM ── Create log directory ─────────────────────────────────────
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM ── Install service ──────────────────────────────────────────
echo Installing %SERVICE_NAME% as a Windows service...

"%NSSM_PATH%" install %SERVICE_NAME% "%PYTHON_EXE%" "main.py"

REM Working directory
"%NSSM_PATH%" set %SERVICE_NAME% AppDirectory "%BOT_DIR%"

REM Stdout / Stderr logs
"%NSSM_PATH%" set %SERVICE_NAME% AppStdout  "%LOG_DIR%\bot_stdout.log"
"%NSSM_PATH%" set %SERVICE_NAME% AppStderr  "%LOG_DIR%\bot_stderr.log"
"%NSSM_PATH%" set %SERVICE_NAME% AppRotateFiles 1
"%NSSM_PATH%" set %SERVICE_NAME% AppRotateSeconds 86400
"%NSSM_PATH%" set %SERVICE_NAME% AppRotateBytes  10485760

REM Restart policy: always restart on exit, 5 s delay
"%NSSM_PATH%" set %SERVICE_NAME% AppExit  Default Restart
"%NSSM_PATH%" set %SERVICE_NAME% AppRestartDelay 5000

REM Environment — reads from .env file via python-dotenv at startup
REM Add extra env vars here if not using .env:
REM   "%NSSM_PATH%" set %SERVICE_NAME% AppEnvironmentExtra "PAPER_MODE=1"

REM Service description
"%NSSM_PATH%" set %SERVICE_NAME% Description "confluence_bot crypto trading bot"

REM Start type: automatic (starts on Windows boot)
"%NSSM_PATH%" set %SERVICE_NAME% Start SERVICE_AUTO_START

REM ── Start the service ────────────────────────────────────────
echo Starting %SERVICE_NAME%...
"%NSSM_PATH%" start %SERVICE_NAME%

echo.
echo [OK] Service installed and started.
echo      Dashboard: http://localhost:8000
echo      Logs:      %LOG_DIR%\bot_stdout.log
echo.
echo To stop:    sc stop  %SERVICE_NAME%
echo To start:   sc start %SERVICE_NAME%
echo To remove:  install_service.bat remove
echo.
pause
