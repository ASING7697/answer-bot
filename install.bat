@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Notion Photo Uploader - install
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m pip install -r requirements.txt
) else (
    python -m pip install -r requirements.txt
)
echo.
echo If you see "Successfully installed" or "Requirement already satisfied", you are ready.
pause
