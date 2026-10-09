@echo off
cd /d "%~dp0"
rem Sesli asistan (tepsi + uyandirma sozcugu). Hatalar logs\artemis.log dosyasina yazilir.
start "" pythonw main.py --voice
