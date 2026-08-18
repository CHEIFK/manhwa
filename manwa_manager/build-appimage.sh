#!/bin/bash
set -e
cd "$(dirname "$0")"
python3 -m pip install --user pyinstaller -r requirements.txt
rm -rf build dist AppDir
mkdir -p AppDir/usr/bin AppDir/usr/share/applications AppDir/usr/share/icons/hicolor/scalable/apps
pyinstaller --clean --noconfirm ManwaManager.spec
cp -a dist/ManwaManager AppDir/usr/bin/ManwaManager
cp index.html AppDir/usr/bin/index.html
cp ManwaManager.desktop AppDir/usr/share/applications/
cp assets/manwamanager.svg AppDir/usr/share/icons/hicolor/scalable/apps/
cp scripts/AppRun AppDir/AppRun
chmod +x AppDir/AppRun
if ! command -v appimagetool >/dev/null 2>&1; then
  echo "appimagetool is required. Download it from https://github.com/AppImage/appimagetool/releases"
  exit 1
fi
ARCH=x86_64 appimagetool AppDir ManwaManager-x86_64.AppImage
echo "Created: ManwaManager-x86_64.AppImage"
