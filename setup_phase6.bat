@echo off
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
python -m playwright install chromium
if errorlevel 1 exit /b 1
echo.
echo Phase 6 dependencies installed. Run: python main.py
