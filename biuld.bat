@echo off
rem Builds dist\ComNotifier.exe: a single EXE with no external files.
cd /d "%~dp0"

python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto error

python -m PyInstaller --noconfirm --clean --onefile --noconsole --name ComNotifier --icon icon.ico --exclude-module tkinter com_notifier.py
if errorlevel 1 goto error

echo.
echo Done: %~dp0dist\ComNotifier.exe
pause
exit /b 0

:error
echo.
echo Build failed, see messages above.
pause
exit /b 1
