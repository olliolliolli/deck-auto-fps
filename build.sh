#!/bin/bash
# Збирає AutoFPS.zip, готовий до "Install Plugin from Zip" у Decky.
# Потрібен Node.js 18+ (на ПК або в Desktop Mode).
set -e
cd "$(dirname "$0")"
npm install --no-audit --no-fund
npx tsc --noEmit -p .
npm run build
rm -rf out && mkdir -p out/AutoFPS
cp -r dist plugin.json package.json main.py README.md LICENSE out/AutoFPS/
cp -r defaults/* out/AutoFPS/
# міст FSR2→FSR4 для RE Engine (Windows DLL); без MinGW плагін збирається без нього
if command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1; then
  sh native/fsr2bridge/build.sh
  mkdir -p out/AutoFPS/bin/fsr2bridge
  cp native/fsr2bridge/out/dinput8.dll out/AutoFPS/bin/fsr2bridge/dinput8.dll
else
  echo "⚠ MinGW не знайдено — міст FSR2→FSR4 не включено"
fi
(cd out && zip -qr ../AutoFPS.zip AutoFPS)
echo "Готово: $(pwd)/AutoFPS.zip"
