#!/bin/bash
set -e

SERVICE_NAME="recorder_server"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

if [ ! -f "$SERVICE_FILE" ]; then
    echo "Error: Service $SERVICE_NAME is not installed."
    exit 1
fi

echo "Uninstalling $SERVICE_NAME systemd service..."

# Stop and disable the service
sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true

# Remove the unit file
sudo rm -f "$SERVICE_FILE"

# Reload systemd
sudo systemctl daemon-reload

echo "Service $SERVICE_NAME uninstalled."
