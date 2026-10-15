@echo off
REM Builds a single windowed (no console) rip.exe with HandBrakeCLI (+ optional extras) embedded.
REM HandBrakeCLI.exe: if it is not in this folder, version 1.11.2 is downloaded from
REM   the official HandBrake GitHub release and checked against a pinned SHA-256.
REM Optional, embedded if present:
REM   rippy.ico / rippy.png (the Rippy logo: exe icon, window icon and taskbar icon)
REM   libdvdcss-2.dll       (DVD decryption). If missing and MSYS2 is installed in C:\msys64,
REM                         it is compiled from VideoLAN's official source (get_libdvdcss.sh).
REM   movie_profile.json    (your HandBrake preset)
REM   tv_profile.json       (optional HandBrake preset for TV shows; else the movie preset is used)
REM   MakeMKV\              (copy of C:\Program Files (x86)\MakeMKV, for Blu-ray)
REM   nas.json              (NAS login for uploads; copy nas.json.example and fill it in)
set HB_VER=1.11.2
set HB_SHA256=80bfe8d5f5d11cc3ef76b834add3ed4e82dee6523ffeb435c283f88b1a21f09d
set HB_URL=https://github.com/HandBrake/HandBrake/releases/download/%HB_VER%/HandBrakeCLI-%HB_VER%-win-x86_64.zip
if exist HandBrakeCLI.exe goto have_hb
echo HandBrakeCLI.exe not found. Downloading HandBrakeCLI %HB_VER%...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing '%HB_URL%' -OutFile hb_download.zip; if ((Get-FileHash hb_download.zip -Algorithm SHA256).Hash -ne '%HB_SHA256%') { Write-Host 'Checksum does not match, refusing to use the download.'; exit 2 }; Expand-Archive -Force hb_download.zip -DestinationPath hb_download; Copy-Item hb_download\HandBrakeCLI.exe HandBrakeCLI.exe -Force"
set HB_ERR=%errorlevel%
del /f /q hb_download.zip >nul 2>&1
if exist hb_download rmdir /s /q hb_download
if not %HB_ERR%==0 goto hb_failed
if not exist HandBrakeCLI.exe goto hb_failed
goto have_hb
:hb_failed
echo.
echo Could not download HandBrakeCLI. Download it yourself from handbrake.fr and put HandBrakeCLI.exe in this folder.
if not defined CI pause
exit /b 1
:have_hb
if exist libdvdcss-2.dll goto have_dvdcss
if not exist C:\msys64\usr\bin\bash.exe goto no_dvdcss
echo libdvdcss-2.dll not found. Building it from VideoLAN source with MSYS2...
set MSYSTEM=MINGW64
set CHERE_INVOKING=1
C:\msys64\usr\bin\bash.exe -lc "bash get_libdvdcss.sh"
if exist libdvdcss-2.dll goto have_dvdcss
:no_dvdcss
echo.
echo NOTE: no libdvdcss-2.dll, so encrypted DVDs will not work in this build.
echo   VideoLAN only publishes libdvdcss as source. Either put libdvdcss-2.dll in this folder,
echo   or install MSYS2 (msys2.org) to C:\msys64 and run this again to compile it automatically.
echo.
:have_dvdcss
python -m pip install pyinstaller smbprotocol pystray pillow
set EXTRA=
if exist rippy.ico set EXTRA=%EXTRA% --icon "rippy.ico" --add-data "rippy.ico;."
if exist rippy.png set EXTRA=%EXTRA% --add-data "rippy.png;."
if exist libdvdcss-2.dll set EXTRA=%EXTRA% --add-binary "libdvdcss-2.dll;."
if exist movie_profile.json set EXTRA=%EXTRA% --add-data "movie_profile.json;."
if exist tv_profile.json set EXTRA=%EXTRA% --add-data "tv_profile.json;."
if exist MakeMKV set EXTRA=%EXTRA% --add-data "MakeMKV;MakeMKV"
if exist nas.json (set EXTRA=%EXTRA% --add-data "nas.json;.") else echo NOTE: no nas.json found, so NAS upload will be off unless you put one next to the exe.
if not exist rippy.ico echo NOTE: no rippy.ico found, so the exe will use the default icon.
taskkill /f /im rip.exe >nul 2>&1
if exist dist\rip.exe del /f /q dist\rip.exe
python -m PyInstaller --onefile --noconsole --add-binary "HandBrakeCLI.exe;." --collect-all smbclient --collect-all smbprotocol --collect-all spnego --hidden-import pystray._win32 %EXTRA% rip.py
if errorlevel 1 (
  echo.
  echo BUILD FAILED. See the messages above.
  if not defined CI pause
  exit /b 1
)
echo.
echo Done. Your exe is in the dist folder.
if not defined CI pause
