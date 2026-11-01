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
(cd out && zip -qr ../AutoFPS.zip AutoFPS)
echo "Готово: $(pwd)/AutoFPS.zip"
