@echo off
rem RigBridge - starts in the system tray (no console window).
cd /d "%~dp0"
start "" pyw -3 "%~dp0rigbridge.py"
