#!/bin/bash
set -e

SERVICE_NAME="recorder_server"
SERVICE_FILE="/etc/init.d/$SERVICE_NAME"
WORKING_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$WORKING_DIR/.venv/bin/python"
PORT="${1:-3000}"

if [ ! -f "$PYTHON" ]; then
    echo "Error: Python venv not found. Run setup.sh first."
    exit 1
fi

echo "Installing $SERVICE_NAME init.d service..."

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
#!/bin/sh
### BEGIN INIT INFO
# Provides:          $SERVICE_NAME
# Required-Start:    \$network \$remote_fs
# Required-Stop:     \$network \$remote_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: FFMPEG Recorder Web Server
# Description:       Runs the FFMPEG Recorder uvicorn web server
### END INIT INFO

WORKING_DIR="$WORKING_DIR"
PYTHON="$PYTHON"
PORT="$PORT"
PIDFILE="/var/run/$SERVICE_NAME.pid"
LOGFILE="/var/log/$SERVICE_NAME.log"

case "\$1" in
    start)
        echo "Starting $SERVICE_NAME..."
        if [ -f "\$PIDFILE" ] && kill -0 \$(cat "\$PIDFILE") 2>/dev/null; then
            echo "$SERVICE_NAME is already running."
            exit 0
        fi
        cd "\$WORKING_DIR"
        nohup "\$PYTHON" -m uvicorn recorder_server:app --host 0.0.0.0 --port "\$PORT" > "\$LOGFILE" 2>&1 &
        echo \$! > "\$PIDFILE"
        echo "$SERVICE_NAME started."
        ;;
    stop)
        echo "Stopping $SERVICE_NAME..."
        if [ -f "\$PIDFILE" ]; then
            kill \$(cat "\$PIDFILE") 2>/dev/null || true
            rm -f "\$PIDFILE"
            echo "$SERVICE_NAME stopped."
        else
            echo "$SERVICE_NAME is not running."
        fi
        ;;
    restart)
        \$0 stop
        sleep 1
        \$0 start
        ;;
    status)
        if [ -f "\$PIDFILE" ] && kill -0 \$(cat "\$PIDFILE") 2>/dev/null; then
            echo "$SERVICE_NAME is running (PID \$(cat "\$PIDFILE"))."
        else
            echo "$SERVICE_NAME is not running."
            exit 1
        fi
        ;;
    *)
        echo "Usage: \$0 {start|stop|restart|status}"
        exit 1
        ;;
esac
exit 0
EOF

sudo chmod +x "$SERVICE_FILE"
sudo update-rc.d "$SERVICE_NAME" defaults

echo "Service installed. Usage:"
echo "  sudo service $SERVICE_NAME start"
echo "  sudo service $SERVICE_NAME stop"
echo "  sudo service $SERVICE_NAME restart"
echo "  sudo service $SERVICE_NAME status"
