#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="odysseus.service"

if [[ ! -f "$DIR/$SERVICE" ]]; then
    echo "ERROR: $SERVICE not found in $DIR"
    exit 1
fi

echo "Installing Odysseus systemd service..."
echo ""

# Stop any running instance first
if systemctl is-active odysseus &>/dev/null; then
    echo "Stopping running instance..."
    sudo systemctl stop odysseus
fi

# Install and enable
sudo cp "$DIR/$SERVICE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable odysseus

echo ""
echo "Service installed and enabled."
echo ""
echo "Commands:"
echo "  sudo systemctl start odysseus     # Start now"
echo "  sudo systemctl stop odysseus      # Stop now"
echo "  sudo systemctl restart odysseus   # Restart"
echo "  systemctl status odysseus         # Check status"
echo "  journalctl -u odysseus -f         # View logs"
