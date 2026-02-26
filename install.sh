#!/bin/bash
set -e

SERVICE_NAME="recorder_server"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"
WORKING_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$WORKING_DIR/.venv/bin/python"
PORT="${1:-3000}"

if [ ! -f "$PYTHON" ]; then
    echo "Error: Python venv not found. Run setup.sh first."
    exit 1
fi

echo "Installing $SERVICE_NAME systemd service..."

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=FFMPEG Recorder Web Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$WORKING_DIR
ExecStart=$PYTHON -m uvicorn recorder_server:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"

echo "Service installed. Usage:"
echo "  sudo systemctl start $SERVICE_NAME"
echo "  sudo systemctl stop $SERVICE_NAME"
echo "  sudo systemctl restart $SERVICE_NAME"
echo "  sudo systemctl status $SERVICE_NAME"
echo "  sudo journalctl -u $SERVICE_NAME -f    # view logs"
