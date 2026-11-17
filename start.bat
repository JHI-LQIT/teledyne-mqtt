@echo off
cd /d "%~dp0"
venv\Scripts\python.exe waveform_pub\waveform_pub.py %*
