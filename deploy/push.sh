#!/bin/sh
# Run from your computer: copies the app to the Pi and (re)installs it.
# Usage: deploy/push.sh [pi@turntable.local]
set -e
HOST=${1:-pi@turntable.local}
cd "$(dirname "$0")/.."
rsync -a --delete --exclude __pycache__ app/ "$HOST":turntable/
rsync -a deploy/ "$HOST":turntable-deploy/
ssh "$HOST" 'cd ~/turntable-deploy && ./install.sh'
