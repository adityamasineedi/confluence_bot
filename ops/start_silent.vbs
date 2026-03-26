' Launch confluence_bot watchdog silently (no console window)
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\projects\confluence_bot"
WshShell.Run "powershell -WindowStyle Hidden -Command ""Start-Process 'C:\projects\confluence_bot\venv\Scripts\python.exe' -ArgumentList 'C:\projects\confluence_bot\ops\watchdog.py' -WorkingDirectory 'C:\projects\confluence_bot' -WindowStyle Hidden""", 0, False
Set WshShell = Nothing
