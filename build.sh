#!/usr/bin/env bash
# Builds a single rip executable with HandBrakeCLI (+ optional extras) embedded.
# Needs in this folder: HandBrakeCLI
# Optional, embedded if present: movie_profile.json, MakeMKV/ (folder)
set -e
python3 -m pip install pyinstaller
EXTRA=()
[ -f movie_profile.json ] && EXTRA+=(--add-data "movie_profile.json:.")
[ -d MakeMKV ] && EXTRA+=(--add-data "MakeMKV:MakeMKV")
python3 -m PyInstaller --onefile --add-binary "HandBrakeCLI:." "${EXTRA[@]}" rip.py
echo "Done. Your executable is in the dist folder."
