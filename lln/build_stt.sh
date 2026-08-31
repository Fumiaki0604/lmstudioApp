#!/bin/bash
# stt.swift をビルドし、stt.app としてパッケージ化・署名する。
# bundle idは com.fumiaki.SttApp のまま固定すること
# (マイク・音声認識の許可がこの識別子に対して既に付与されているため)
set -e
cd "$(dirname "$0")"

APP=stt.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

swiftc stt.swift -o "$APP/Contents/MacOS/stt"

cat > "$APP/Contents/Info.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>stt</string>
    <key>CFBundleIdentifier</key>
    <string>com.fumiaki.SttApp</string>
    <key>CFBundleName</key>
    <string>stt</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>音声会話のためマイク入力を使用します。</string>
    <key>NSSpeechRecognitionUsageDescription</key>
    <string>あなたの発話を認識して会話するために使用します。</string>
</dict>
</plist>
EOF

codesign --force -s - "$APP"
echo "built: $APP"
