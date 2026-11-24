#!/bin/sh
# Run on the Pi from ~/turntable-deploy. Safe to re-run.
set -e
echo "icecast2 icecast2/icecast-setup boolean false" | sudo debconf-set-selections
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq icecast2 ffmpeg python3-venv python3-numpy python3-aiohttp
[ -d ~/turntable-venv ] || python3 -m venv --system-site-packages ~/turntable-venv
~/turntable-venv/bin/pip install -q soco shazamio
if [ ! -f /etc/turntable.env ]; then
  PW=$(head -c 18 /dev/urandom | base64 | tr -d '/+=')
  ADMIN=$(head -c 18 /dev/urandom | base64 | tr -d '/+=')
  printf 'ICECAST_SOURCE_PASSWORD=%s\nGAIN_DB=0\nVISUAL_DELAY=2.5\n' "$PW" | sudo tee /etc/turntable.env >/dev/null
  sudo chmod 600 /etc/turntable.env
  sudo sed -i -E "s#<source-password>[^<]*#<source-password>$PW#; s#<relay-password>[^<]*#<relay-password>$PW#; s#<admin-password>[^<]*#<admin-password>$ADMIN#" /etc/icecast2/icecast.xml
  sudo systemctl restart icecast2
fi
sudo systemctl enable --now icecast2
sudo install -m 644 turntable.service turntable-audio.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable turntable turntable-audio
# Restart the audio service only when its code or unit changed: restarting it interrupts playback.
STAMP=~/.cache/turntable-audio.deployed
mkdir -p ~/.cache
if ! cat ~/turntable/audio.py turntable-audio.service | cmp -s - "$STAMP"; then
  echo "audio.py changed: restarting the audio service (interrupts playback briefly)"
  sudo systemctl restart turntable-audio
  cat ~/turntable/audio.py turntable-audio.service > "$STAMP"
  # Give the running control app time to put the playing rooms back before it restarts too.
  sleep 30
fi
sudo systemctl restart turntable
# Invisible mouse pointer on the touchscreen (touches otherwise leave an arrow on screen).
python3 blank-cursor.py >/dev/null
grep -q "^XCURSOR_THEME=blank" ~/.config/labwc/environment 2>/dev/null || echo "XCURSOR_THEME=blank" >> ~/.config/labwc/environment
mkdir -p ~/.config/autostart
install -m 644 kiosk.desktop ~/.config/autostart/turntable-kiosk.desktop
