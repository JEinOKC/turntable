#!/bin/sh
# Opens the turntable page full-screen on the Pi's display.
# A power cut leaves Chromium's profile marked "Crashed", which can show a blank page; clear it first.
sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/' ~/.config/chromium/Default/Preferences 2>/dev/null
until curl -sf http://localhost/api/status >/dev/null; do sleep 2; done
exec chromium --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble --no-first-run \
  --password-store=basic --ozone-platform=wayland "http://localhost/?kiosk"
