#!/usr/bin/env bash
# Builds libdvdcss-2.dll (64-bit Windows) from VideoLAN's official source release.
# VideoLAN only publishes source for libdvdcss now, so this compiles it with MSYS2/MinGW.
# Run it from an MSYS2 "MINGW64" shell in the folder where the DLL should end up.
# (build.bat runs it for you if MSYS2 is installed in C:\msys64; GitHub Actions runs it too.)
set -euo pipefail

VER=1.6.0
BASE="https://download.videolan.org/pub/libdvdcss/$VER"
OUT_DIR="$(pwd)"

if ! command -v meson >/dev/null 2>&1 || ! command -v gcc >/dev/null 2>&1; then
  pacman -S --needed --noconfirm mingw-w64-x86_64-gcc mingw-w64-x86_64-meson mingw-w64-x86_64-ninja
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

echo "Downloading libdvdcss $VER source from VideoLAN..."
curl -fsSL -O "$BASE/libdvdcss-$VER.tar.xz"
curl -fsSL -O "$BASE/libdvdcss-$VER.tar.xz.sha256"

EXPECTED="$(awk '{print $1}' "libdvdcss-$VER.tar.xz.sha256")"
ACTUAL="$(sha256sum "libdvdcss-$VER.tar.xz" | awk '{print $1}')"
if [ -z "$EXPECTED" ] || [ "$EXPECTED" != "$ACTUAL" ]; then
  echo "Checksum does not match, refusing to build the download." >&2
  exit 1
fi

tar -xf "libdvdcss-$VER.tar.xz"
cd "libdvdcss-$VER"
meson setup build --buildtype=release --default-library=shared -Dc_link_args=-static-libgcc
ninja -C build

DLL="$(find build -name 'libdvdcss*.dll' | head -n 1)"
if [ -z "$DLL" ]; then
  echo "Build finished but no libdvdcss DLL was produced." >&2
  exit 1
fi
strip "$DLL" || true
cp "$DLL" "$OUT_DIR/libdvdcss-2.dll"
echo "Built $OUT_DIR/libdvdcss-2.dll"
