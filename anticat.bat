@echo off
setlocal
rem Run anticat on Windows. First run creates the virtual environment and installs dependencies.
rem Any arguments are passed through, e.g.:  anticat.bat --sound miau.mp3 --snapshots capturas
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

echo [anticat] First run: creating virtual environment...
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo [anticat] Could not create the virtual environment.
    echo [anticat] Install Python 3.10+ from https://www.python.org and tick "Add python.exe to PATH".
    goto fail
)

echo [anticat] Installing dependencies, this can take several minutes...
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto install_fail
".venv\Scripts\python.exe" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu || goto install_fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto install_fail
echo [anticat] Setup complete.

:run
".venv\Scripts\python.exe" anticat.py %*
if errorlevel 1 goto fail
exit /b 0

:install_fail
echo [anticat] Dependency installation failed (check your internet connection), then run this file again.
rmdir /s /q .venv 2>nul

:fail
echo.
pause
exit /b 1
