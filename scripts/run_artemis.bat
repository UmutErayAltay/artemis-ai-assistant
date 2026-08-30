@echo off
REM Artemis'i sesli asistan modunda baslatir (bkz. main.py --voice).
REM Masaustu kisayolu bu dosyayi hedef gosterir; proje nereye tasinirsa
REM tasinsin %~dp0 sayesinde kendi klasorune gore calisir.
cd /d "%~dp0\.."
python main.py --voice
if errorlevel 1 (
    echo.
    echo Artemis bir hatayla kapandi ^(yukarida^). Pencereyi kapatmak icin bir tusa basin.
    pause >nul
)
