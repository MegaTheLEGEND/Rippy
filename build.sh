#!/usr/bin/env bash
# Builds a single rip executable with HandBrakeCLI (+ optional extras) embedded.
# HandBrakeCLI: if it is not in this folder, it is fetched automatically
#   macOS: downloads HandBrakeCLI 1.11.2 from the official GitHub release (checksum-verified)
#   Linux: HandBrake has no official CLI binary, so it copies the one on your PATH (apt/dnf install)
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

HB_VER=1.11.2
HB_DMG_SHA256=14463aa81038aaa3ce421dc6cee65fd6c82fdabda040931541ccca38939299fa
if [ ! -f HandBrakeCLI ]; then
  echo "HandBrakeCLI not found in this folder."
  case "$(uname)" in
    Darwin)
      echo "Downloading HandBrakeCLI $HB_VER..."
      curl -fL -o hb_download.dmg "https://github.com/HandBrake/HandBrake/releases/download/$HB_VER/HandBrakeCLI-$HB_VER.dmg"
      echo "$HB_DMG_SHA256  hb_download.dmg" | shasum -a 256 -c - || { rm -f hb_download.dmg; echo "Checksum does not match, refusing to use the download."; exit 1; }
      MNT="$(mktemp -d)"
      hdiutil attach hb_download.dmg -nobrowse -quiet -mountpoint "$MNT"
      FOUND="$(find "$MNT" -name HandBrakeCLI -type f | head -n 1)"
      [ -n "$FOUND" ] && cp "$FOUND" HandBrakeCLI
      hdiutil detach "$MNT" -quiet || true
      rm -f hb_download.dmg
      ;;
    *)
      if command -v HandBrakeCLI >/dev/null 2>&1; then
        echo "Using the HandBrakeCLI found on your PATH: $(command -v HandBrakeCLI)"
        cp "$(command -v HandBrakeCLI)" HandBrakeCLI
      fi
      ;;
  esac
  if [ ! -f HandBrakeCLI ]; then
    echo "Could not get HandBrakeCLI. Install it (Linux: apt install handbrake-cli, or your distro's package)"
    echo "or download it from handbrake.fr, and put HandBrakeCLI in this folder."
    exit 1
  fi
  chmod +x HandBrakeCLI
fi
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
