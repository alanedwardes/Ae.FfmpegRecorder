#!/bin/bash
set -e

SERVICE_NAME="recorder_server"
SERVICE_FILE="/etc/init.d/$SERVICE_NAME"

if [ ! -f "$SERVICE_FILE" ]; then
    echo "Error: Service $SERVICE_NAME is not installed."
    exit 1
fi

echo "Uninstalling $SERVICE_NAME init.d service..."

# Stop the service if running
sudo service "$SERVICE_NAME" stop 2>/dev/null || true

# Remove from startup
sudo update-rc.d -f "$SERVICE_NAME" remove

# Remove the init.d script
sudo rm -f "$SERVICE_FILE"

echo "Service $SERVICE_NAME uninstalled."
