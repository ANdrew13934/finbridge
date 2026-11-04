#!/bin/sh
# Started by FinBridge.app (launchd, every 5 min). FinBridge.app holds the
# Full Disk Access permission needed to read SMS from Messages.
cd "$(dirname "$0")" || exit 1
for PY in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
          /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$PY" ] && break
done
exec "$PY" -I bridge.py "$@" >> launchd.log 2>&1
