#!/bin/sh
# Run from your computer: copies the app to the Pi and (re)installs it.
# Usage: deploy/push.sh [pi@turntable.local] [--audio]
set -e
HOST=${1:-pi@turntable.local}
cd "$(dirname "$0")/.."
# Changing audio.py restarts the audio service, which interrupts playback. Require a flag for it.
if ! ssh "$HOST" cat turntable/audio.py 2>/dev/null | cmp -s - app/audio.py && [ "$2" != "--audio" ]; then
  echo "audio.py changed: deploying it interrupts playback. Re-run with --audio to go ahead." >&2
  exit 1
fi
rsync -a --delete --exclude __pycache__ app/ "$HOST":turntable/
rsync -a deploy/ "$HOST":turntable-deploy/
ssh "$HOST" 'cd ~/turntable-deploy && ./install.sh'
