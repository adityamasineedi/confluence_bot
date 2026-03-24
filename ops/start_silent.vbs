' Launch confluence_bot watchdog silently (no console window)
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\projects\confluence_bot"
WshShell.Run """C:\projects\confluence_bot\venv\Scripts\python.exe"" ""C:\projects\confluence_bot\ops\watchdog.py""", 0, False
Set WshShell = Nothing
