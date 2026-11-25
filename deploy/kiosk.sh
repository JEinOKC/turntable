#!/bin/sh
# Opens the turntable page full-screen on the Pi's display, and restarts the browser if the
# page stops checking in (after hours of running it can stall on a white screen).

# Only one launcher at a time: a second one would keep opening extra windows in the first's browser.
exec 9>/tmp/turntable-kiosk.lock
flock -n 9 || { echo "kiosk launcher already running"; exit 0; }

until curl -sf http://localhost/api/status >/dev/null; do sleep 2; done
while true; do
  pkill -x chromium 2>/dev/null && sleep 2  # never start alongside a leftover browser
  # A power cut leaves Chromium's profile marked "Crashed", which can show a blank page; clear it.
  sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/' ~/.config/chromium/Default/Preferences 2>/dev/null
  chromium --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble --no-first-run \
    --password-store=basic --ozone-platform=wayland "http://localhost/?kiosk" &
  BROWSER=$!
  sleep 60  # time to start up
  misses=0
  while kill -0 $BROWSER 2>/dev/null; do
    if curl -sf -m 5 http://localhost/api/kiosk-alive >/dev/null; then
      misses=0
    elif curl -sf -m 5 http://localhost/api/status >/dev/null; then
      misses=$((misses + 1))  # the app is up but the page isn't checking in
      if [ $misses -ge 3 ]; then
        echo "$(date): page stopped checking in; restarting the browser"
        kill $BROWSER
      fi
    fi
    sleep 15
  done
  wait $BROWSER 2>/dev/null
  sleep 3
done
