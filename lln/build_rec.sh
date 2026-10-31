#!/bin/bash
# rec.swift をビルドし、rec.app としてパッケージ化・署名する。
# bundle idは com.fumiaki.RecApp で固定すること(マイク・画面収録の許可が
# この識別子と署名に対して付与されるため。変えると許可を取り直しになる)
set -e
cd "$(dirname "$0")"

APP=rec.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

swiftc rec.swift -o "$APP/Contents/MacOS/rec"

cat > "$APP/Contents/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>rec</string>
    <key>CFBundleIdentifier</key>
    <string>com.fumiaki.RecApp</string>
    <key>CFBundleName</key>
    <string>rec</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>セミナーの音声を録音して要約するために使用します。</string>
</dict>
</plist>
EOF

codesign --force -s - "$APP"
echo "built: $APP"
