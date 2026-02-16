# Ae.FfmpegRecorder

FastAPI wrapper for ffmpeg recording (V4L2/ALSA).


## System Requirements

```bash
sudo apt install ffmpeg alsa-utils v4l-utils
cc usbreset.c -o usbreset && sudo mv usbreset /usr/local/bin/
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running

```bash
./run.sh
```

Server runs on port 3000.
