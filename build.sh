#!/usr/bin/env bash
# Builds a single rip executable with HandBrakeCLI (+ optional extras) embedded.
# Needs in this folder: HandBrakeCLI
# Optional, embedded if present:
#   rippy.png / rippy.ico   the Rippy logo (window and taskbar icon)
#   rippy.icns              exe/app icon on macOS (PyInstaller wants .icns there)
#   movie_profile.json      your HandBrake preset
#   tv_profile.json         HandBrake preset for TV shows (else the movie preset is used)
#   MakeMKV/                (folder) for Blu-ray, if you have a copy
#   nas.json                NAS login for uploads (copy nas.json.example and fill it in)
# libdvdcss can't be embedded on Mac/Linux; install it on the system (brew install libdvdcss / libdvd-pkg).
set -e
python3 -m pip install pyinstaller smbprotocol
EXTRA=()
[ -f rippy.png ] && EXTRA+=(--add-data "rippy.png:.")
[ -f rippy.ico ] && EXTRA+=(--add-data "rippy.ico:.")
if [ "$(uname)" = "Darwin" ]; then
  [ -f rippy.icns ] && EXTRA+=(--icon "rippy.icns")
else
  [ -f rippy.ico ] && EXTRA+=(--icon "rippy.ico")
fi
[ -f movie_profile.json ] && EXTRA+=(--add-data "movie_profile.json:.")
[ -f tv_profile.json ] && EXTRA+=(--add-data "tv_profile.json:.")
[ -d MakeMKV ] && EXTRA+=(--add-data "MakeMKV:MakeMKV")
if [ -f nas.json ]; then
  EXTRA+=(--add-data "nas.json:.")
else
  echo "NOTE: no nas.json found, so NAS upload will be off unless you put one next to the executable."
fi
[ -f rippy.png ] || echo "NOTE: no rippy.png found, so the window will use the default icon."
python3 -m PyInstaller --onefile --add-binary "HandBrakeCLI:." \
  --collect-all smbclient --collect-all smbprotocol --collect-all spnego \
  "${EXTRA[@]}" rip.py
echo "Done. Your executable is in the dist folder."
