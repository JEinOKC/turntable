#!/bin/sh
# Run on the Pi from ~/turntable-deploy. Safe to re-run.
set -e
if [ ! -f /etc/turntable.env ]; then
  PW=$(head -c 18 /dev/urandom | base64 | tr -d '/+=')
  ADMIN=$(head -c 18 /dev/urandom | base64 | tr -d '/+=')
  printf 'ICECAST_SOURCE_PASSWORD=%s\nGAIN_DB=0\nVISUAL_DELAY=2.5\n' "$PW" | sudo tee /etc/turntable.env >/dev/null
  sudo chmod 600 /etc/turntable.env
  sudo sed -i -E "s#<source-password>[^<]*#<source-password>$PW#; s#<relay-password>[^<]*#<relay-password>$PW#; s#<admin-password>[^<]*#<admin-password>$ADMIN#" /etc/icecast2/icecast.xml
  sudo systemctl restart icecast2
fi
sudo systemctl enable --now icecast2
sudo install -m 644 turntable.service /etc/systemd/system/turntable.service
sudo systemctl daemon-reload
sudo systemctl enable turntable
sudo systemctl restart turntable
mkdir -p ~/.config/autostart
install -m 644 kiosk.desktop ~/.config/autostart/turntable-kiosk.desktop
