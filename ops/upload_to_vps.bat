@echo off
REM ============================================================
REM  Upload confluence_bot to VPS
REM  Usage: upload_to_vps.bat YOUR_SERVER_IP
REM ============================================================

if "%1"=="" (
    echo Usage: upload_to_vps.bat SERVER_IP
    pause
    exit /b 1
)

set SERVER_IP=%1
set BOT_DIR=C:\projects\confluence_bot
set REMOTE_DIR=/opt/confluence_bot

echo Uploading confluence_bot to %SERVER_IP%...

REM Upload all files except venv, __pycache__, DB files, and logs
scp -r %BOT_DIR%\core          root@%SERVER_IP%:%REMOTE_DIR%\
scp -r %BOT_DIR%\signals       root@%SERVER_IP%:%REMOTE_DIR%\
scp -r %BOT_DIR%\data          root@%SERVER_IP%:%REMOTE_DIR%\
scp -r %BOT_DIR%\notifications root@%SERVER_IP%:%REMOTE_DIR%\
scp -r %BOT_DIR%\logging_      root@%SERVER_IP%:%REMOTE_DIR%\
scp -r %BOT_DIR%\backtest      root@%SERVER_IP%:%REMOTE_DIR%\
scp -r %BOT_DIR%\ops           root@%SERVER_IP%:%REMOTE_DIR%\
scp    %BOT_DIR%\main.py       root@%SERVER_IP%:%REMOTE_DIR%\
scp    %BOT_DIR%\config.yaml   root@%SERVER_IP%:%REMOTE_DIR%\
scp    %BOT_DIR%\requirements.txt root@%SERVER_IP%:%REMOTE_DIR%\

REM Upload .env (contains API keys — keep private)
scp    %BOT_DIR%\.env          root@%SERVER_IP%:%REMOTE_DIR%\

echo.
echo [OK] Files uploaded.
echo Now SSH into the server and run:
echo   ssh root@%SERVER_IP%
echo   cd /opt/confluence_bot
echo   bash ops/deploy.sh
echo.
pause
