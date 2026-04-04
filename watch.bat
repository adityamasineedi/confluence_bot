@echo off
echo Watching logs\bot.log for signals...
echo.
cd /d %~dp0
powershell -Command "Get-Content logs\bot.log -Wait | Select-String 'FIRE|PAPER|SHORT|LONG|BR |regime|balance|BLOCKED|ERROR'"
