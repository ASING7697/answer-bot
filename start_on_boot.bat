@echo off
cd /d "%~dp0"
echo Adding the Telegram listener to Windows start-up...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\Notion Photo Uploader (Telegram).lnk'); $s.TargetPath='%~dp0run_telegram.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
if %errorlevel%==0 (
    echo Done. The listener will start by itself, minimised, every time you log in to Windows.
    echo To undo this, double-click remove_from_boot.bat
) else (
    echo Could not add it. Follow the manual steps in SETUP.md instead.
)
pause
