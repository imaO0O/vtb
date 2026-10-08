@echo off
cd /d "%~dp0.."
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python scripts\run_chat.py
) else (
    python scripts\run_chat.py
)
