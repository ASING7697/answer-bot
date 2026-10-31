@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Notion Photo Uploader
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 notion_attach.py %*
) else (
    python notion_attach.py %*
)
echo.
pause
