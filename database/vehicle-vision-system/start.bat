@echo off
cd /d "%~dp0"

set "PYTHON_EXE=%USERPROFILE%\anaconda3\envs\sqlquery311\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

set PORT_PID=
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":8001 .*LISTENING"') do set PORT_PID=%%a
if not defined PORT_PID goto START_SERVER

curl.exe -k -sS --max-time 3 https://localhost:8001/ >nul 2>&1
if not errorlevel 1 goto HTTPS_RUNNING

echo Port 8001 is occupied by an old non-HTTPS server. Restarting it securely...
powershell -NoProfile -Command "$root=%PORT_PID%; $all=Get-CimInstance Win32_Process; foreach($p in $all){if($p.ParentProcessId -eq $root){Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue}}; Stop-Process -Id $root -Force -ErrorAction SilentlyContinue"
timeout /t 2 /nobreak >nul
set PORT_PID=
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":8001 .*LISTENING"') do set PORT_PID=%%a
if defined PORT_PID goto PORT_BUSY

:START_SERVER

"%PYTHON_EXE%" -c "import fastapi, uvicorn, sqlalchemy, cv2, qrcode, ultralytics" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies...
  "%PYTHON_EXE%" -m pip install -r requirements.txt
  if errorlevel 1 exit /b 1
)

echo Starting server at https://localhost:8001 ...
"%PYTHON_EXE%" run.py
pause
exit /b %errorlevel%

:HTTPS_RUNNING
echo Server is already running on https://localhost:8001
echo PID: %PORT_PID%
pause
exit /b 0

:PORT_BUSY
echo Unable to stop the existing process on port 8001. PID: %PORT_PID%
echo Please close the old server window and run start.bat again.
pause
exit /b 1
