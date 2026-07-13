@echo off
cd /d "%~dp0"

set "PYTHON_EXE=%USERPROFILE%\anaconda3\envs\sqlquery311\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

set PORT_PID=
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":8001 .*LISTENING"') do set PORT_PID=%%a
if defined PORT_PID (
  echo Server is already running on https://localhost:8001
  echo PID: %PORT_PID%
  pause
  exit /b 0
)

"%PYTHON_EXE%" -c "import fastapi, uvicorn, sqlalchemy, cv2, qrcode, ultralytics" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies...
  "%PYTHON_EXE%" -m pip install -r requirements.txt
  if errorlevel 1 exit /b 1
)

echo Starting server at https://localhost:8001 ...
"%PYTHON_EXE%" run.py
pause
