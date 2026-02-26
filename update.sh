#!/bin/bash
set -e

SERVICE_NAME="recorder_server"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$SCRIPT_DIR"

echo "Pulling latest changes..."
git pull

if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "Service is running, restarting..."
    sudo systemctl restart "$SERVICE_NAME"
    echo "Service restarted."
else
    echo "Service is not running, skipping restart."
fi
