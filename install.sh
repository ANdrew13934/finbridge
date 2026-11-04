#!/bin/sh
# Builds ~/Applications/FinBridge.app (the app that gets Full Disk Access) and a
# LaunchAgent that starts it every 5 minutes. Run it again after moving this folder.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="local.finbridge"
APP="$HOME/Applications/FinBridge.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -f "$DIR/config.json" ]; then
  echo "Сначала создай и заполни config.json (см. CLAUDE.md)"
  exit 1
fi
chmod +x "$DIR/run.sh"
mkdir -p "$HOME/Applications" "$HOME/Library/LaunchAgents"

# The app only starts run.sh. Rebuilding it resets its Full Disk Access, so keep it if it already points here.
if [ -d "$APP" ] && osadecompile "$APP/Contents/Resources/Scripts/main.scpt" 2>/dev/null | grep -qF "$DIR/run.sh"; then
  echo "FinBridge.app уже есть: $APP"
else
  rm -rf "$APP"
  osacompile -o "$APP" -e "do shell script \"/bin/sh \" & quoted form of \"$DIR/run.sh\""
  echo "Создано: $APP"
  echo "→ Дай ему Полный доступ к диску: Системные настройки → Конфиденциальность и безопасность → Полный доступ к диску → «+»"
fi

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP/Contents/MacOS/applet</string>
  </array>
  <!-- every 5 minutes while the Mac is awake, at login and after waking from sleep -->
  <key>StartInterval</key>
  <integer>300</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardErrorPath</key>
  <string>$DIR/launchd.err.log</string>
</dict>
</plist>
PLIST
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Автозапуск включён: проверка каждые 5 минут. Журнал: $DIR/bridge.log"
