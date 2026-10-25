#!/bin/bash
# Собирает «Comic Video.app» из файлов этой папки.
set -e
cd "$(dirname "$0")"
APP="Comic Video.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O ocr.swift -o ocr
swiftc -O pdf2png.swift -o pdf2png
cp app.py comic2video.py manga_worker.py index.html ocr pdf2png "$APP/Contents/Resources/"
cat > "$APP/Contents/MacOS/ComicVideo" <<'SH'
#!/bin/bash
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
# на Apple Silicon запускаем python нативно (arm64), иначе он может стартовать под Rosetta
if [ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
  exec /usr/bin/arch -arm64 /usr/bin/python3 "$DIR/app.py"
fi
exec /usr/bin/python3 "$DIR/app.py"
SH
chmod +x "$APP/Contents/MacOS/ComicVideo"
cat > "$APP/Contents/Info.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Comic Video</string>
  <key>CFBundleDisplayName</key><string>Comic Video</string>
  <key>CFBundleIdentifier</key><string>local.comicvideo</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>ComicVideo</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string><string>x86_64</string></array>
  <key>LSRequiresNativeExecution</key><true/>
</dict></plist>
PL
# иконка
/usr/bin/python3 - <<'PY'
from PIL import Image, ImageDraw
im = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
d.rounded_rectangle((90, 90, 934, 934), 190, fill=(47, 111, 237))
d.rounded_rectangle((250, 230, 774, 794), 40, fill=(255, 255, 255))
for y in (330, 420, 510): d.rounded_rectangle((320, y, 704, y + 40), 20, fill=(200, 210, 230))
d.polygon([(470, 590), (470, 730), (590, 660)], fill=(47, 111, 237))
im.save("/tmp/cv_icon.png")
PY
mkdir -p /tmp/cv.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s /tmp/cv_icon.png --out /tmp/cv.iconset/icon_${s}x${s}.png >/dev/null
  sips -z $((s*2)) $((s*2)) /tmp/cv_icon.png --out /tmp/cv.iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns /tmp/cv.iconset -o "$APP/Contents/Resources/icon.icns"
rm -rf /tmp/cv.iconset /tmp/cv_icon.png
echo "Готово: $APP"
