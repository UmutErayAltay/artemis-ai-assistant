@echo off
REM Artemis'i birlesik uygulama olarak baslatir: tepsi + sesli asistan + panel
REM (bkz. main.py; secenek verilmezse --voice ile ayni).
REM Masaustu kisayolu bu dosyayi hedef gosterir; proje nereye tasinirsa
REM tasinsin %~dp0 sayesinde kendi klasorune gore calisir.
cd /d "%~dp0\.."
python main.py
if errorlevel 1 (
    echo.
    echo Artemis bir hatayla kapandi ^(yukarida^). Pencereyi kapatmak icin bir tusa basin.
    pause >nul
)
