@echo off
echo Starting confluence_bot...
echo Logs: logs\bot.log
echo Press Ctrl+C to stop
echo.
cd /d %~dp0
call venv\Scripts\activate
python main.py
pause
