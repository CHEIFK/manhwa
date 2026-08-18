#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

python3 -m pip install --user pyinstaller -r requirements.txt
rm -rf build dist AppDir
mkdir -p AppDir/usr/bin AppDir/usr/share/applications AppDir/usr/share/icons/hicolor/scalable/apps
pyinstaller --clean --noconfirm "$SCRIPT_DIR/ManwaManager.spec"
cp -a dist/ManwaManager AppDir/usr/bin/ManwaManager
cp index.html AppDir/usr/bin/index.html
cp ManwaManager.desktop AppDir/ManwaManager.desktop
cp ManwaManager.desktop AppDir/usr/share/applications/ManwaManager.desktop
cp assets/manwamanager.svg AppDir/manwamanager.svg
cp assets/manwamanager.svg AppDir/.DirIcon
cp assets/manwamanager.svg AppDir/usr/share/icons/hicolor/scalable/apps/manwamanager.svg
cp scripts/AppRun AppDir/AppRun
chmod +x AppDir/AppRun

if ! command -v appimagetool >/dev/null 2>&1; then
  if [ -x "./appimagetool" ]; then
    APPIMAGETOOL="./appimagetool"
  elif [ -x "/tmp/appimagetool" ]; then
    APPIMAGETOOL="/tmp/appimagetool"
  else
    echo "appimagetool not found in PATH or locally. Download it from https://github.com/AppImage/appimagetool/releases"
    exit 1
  fi
else
  APPIMAGETOOL="appimagetool"
fi

ARCH=x86_64 "$APPIMAGETOOL" --appimage-extract-and-run AppDir ManwaManager-x86_64.AppImage || ARCH=x86_64 "$APPIMAGETOOL" AppDir ManwaManager-x86_64.AppImage
echo "Created: $SCRIPT_DIR/ManwaManager-x86_64.AppImage"
