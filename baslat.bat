@echo off
cd /d "%~dp0"
rem Artemis (tepsi + sesli asistan + panel). Hatalar logs\artemis.log dosyasina yazilir.
start "" pythonw main.py
