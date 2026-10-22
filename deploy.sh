#!/bin/bash
# Copy the watcher to the Lightsail server (the "teetime" host in ~/.ssh/config)
# and restart it. Also works on a brand-new Ubuntu server: it installs what's
# missing the first time.
# Usage: ./deploy.sh
#
# Copies watcher.py, the service file, and config.server.json (as the server's
# config.json). Never copies .venv, state.json, control.json or the Mac's config.json.
#
# Secrets (ntfy_topic, control_topic, heartbeat_url) come from the macOS Keychain
# (service "teetime-watcher") and are written to root-only files in
# /etc/teetime-watcher/, which systemd passes to the watcher as credentials.
# To set or change one:
#   security add-generic-password -U -s teetime-watcher -a ntfy_topic -w
set -euo pipefail

SERVER=teetime
DIR=$(cd "$(dirname "$0")" && pwd)
SECRETS=(ntfy_topic control_topic heartbeat_url)

for name in "${SECRETS[@]}"; do
  security find-generic-password -s teetime-watcher -a "$name" -w >/dev/null 2>&1 \
    || { echo "Missing Keychain item teetime-watcher/$name; nothing copied. Add it with:"
         echo "  security add-generic-password -U -s teetime-watcher -a $name -w"; exit 1; }
done

python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$DIR/config.server.json" \
  || { echo "config.server.json is not valid JSON; nothing copied."; exit 1; }

echo "Preparing the server (first run on a new server takes a minute or two)..."
ssh "$SERVER" 'set -e
  mkdir -p ~/teetime-watcher && cd ~/teetime-watcher
  if [ ! -x .venv/bin/python ]; then
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null
    python3 -m venv .venv
  fi
  .venv/bin/pip install -q certifi
  [ "$(timedatectl show -p Timezone --value)" = America/New_York ] || sudo timedatectl set-timezone America/New_York
  # Small servers need swap: without it, nightly Ubuntu updates can run out of
  # memory and knock out DNS/SSH (this happened on the 512 MB plan in Oct 2026).
  if [ ! -f /swapfile ]; then
    sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
    echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
    echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-swappiness.conf >/dev/null && sudo sysctl -q -p /etc/sysctl.d/99-swappiness.conf
  fi
  # Firmware updates are meaningless on a cloud VM, and fwupd used ~125 MB when
  # its timer fired, one of the processes behind the Oct 2026 out-of-memory incident.
  if [ "$(systemctl is-enabled fwupd-refresh.timer 2>/dev/null)" != masked ]; then
    sudo systemctl stop fwupd-refresh.timer fwupd.service 2>/dev/null || true
    sudo systemctl mask -q fwupd.service fwupd-refresh.service fwupd-refresh.timer
  fi'

echo "Sending secrets from the Keychain to root-only files on the server..."
ssh "$SERVER" "sudo install -d -m 700 -o root -g root /etc/teetime-watcher"
for name in "${SECRETS[@]}"; do
  # Value goes over SSH stdin, never on a command line.
  security find-generic-password -s teetime-watcher -a "$name" -w \
    | ssh "$SERVER" "sudo sh -c 'umask 077; cat > /etc/teetime-watcher/$name'"
done

echo "Copying files..."
scp "$DIR/watcher.py" "$DIR/teetime-watcher.service" "$SERVER:~/teetime-watcher/"
scp "$DIR/config.server.json" "$SERVER:~/teetime-watcher/config.json"

echo "Restarting the watcher..."
ssh "$SERVER" "sudo cp ~/teetime-watcher/teetime-watcher.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable -q teetime-watcher && sudo systemctl restart teetime-watcher && systemctl is-active teetime-watcher"

echo "Done. The first check runs in about 30 seconds."
