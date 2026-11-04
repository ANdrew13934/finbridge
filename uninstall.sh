#!/bin/sh
# Stops the background runs and removes FinBridge.app. Data in the «Финансы» app stays as it is.
LABEL="local.finbridge"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/Applications/FinBridge.app"
echo "Автозапуск выключен, FinBridge.app удалён."
echo "Можно ещё убрать FinBridge из списка «Полный доступ к диску» в Системных настройках."
