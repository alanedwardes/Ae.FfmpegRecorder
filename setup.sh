#!/bin/bash
set -e

# Install system packages
sudo apt update
sudo apt install -y ffmpeg alsa-utils v4l-utils python3 python3-pip python3-venv

# Setup python environment
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install -r requirements.txt

echo "Setup complete. Run ./run.sh to start."
