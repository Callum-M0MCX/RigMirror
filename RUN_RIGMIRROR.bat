@echo off
cd /d "%~dp0"
python rigmirror.py
if errorlevel 1 pause
