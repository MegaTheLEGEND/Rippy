@echo off
REM Builds a single windowed (no console) rip.exe with HandBrakeCLI (+ optional extras) embedded.
REM Needs in this folder: HandBrakeCLI.exe
REM Optional, embedded if present:
REM   libdvdcss-2.dll       (DVD decryption)
REM   movie_profile.json    (your HandBrake preset)
REM   MakeMKV\              (copy of C:\Program Files (x86)\MakeMKV, for Blu-ray)
REM   nas.json              (NAS login for uploads; copy nas.json.example and fill it in)
python -m pip install pyinstaller smbprotocol
set EXTRA=
if exist libdvdcss-2.dll set EXTRA=%EXTRA% --add-binary "libdvdcss-2.dll;."
if exist movie_profile.json set EXTRA=%EXTRA% --add-data "movie_profile.json;."
if exist MakeMKV set EXTRA=%EXTRA% --add-data "MakeMKV;MakeMKV"
if exist nas.json (set EXTRA=%EXTRA% --add-data "nas.json;.") else echo NOTE: no nas.json found, so NAS upload will be off unless you put one next to the exe.
taskkill /f /im rip.exe >nul 2>&1
if exist dist\rip.exe del /f /q dist\rip.exe
python -m PyInstaller --onefile --noconsole --add-binary "HandBrakeCLI.exe;." --collect-all smbclient --collect-all smbprotocol --collect-all spnego %EXTRA% rip.py
if errorlevel 1 (
  echo.
  echo BUILD FAILED. See the messages above.
  pause
  exit /b 1
)
echo.
echo Done. Your exe is in the dist folder.
pause