import os
import json
import signal
import subprocess
import threading
import time
import re
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
import glob
import queue
import asyncio
from collections import deque

from contextlib import asynccontextmanager

def signal_handler(signum, frame):
    shutdown_event.set()
    if ffmpeg_process and ffmpeg_process.poll() is None:
        ffmpeg_process.terminate()
    if preview_process and preview_process.poll() is None:
        preview_process.terminate()
    os._exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

app = FastAPI()

# Allow CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RECORDINGS_DIR = "recordings"
SETTINGS_FILE = "settings.json"
SETTINGS_KEYS = {"bitrate", "resolution", "format", "preset", "input_format", "video_device", "audio_device", "advanced_open"}
settings_lock = threading.Lock()

def load_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in SETTINGS_KEYS}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def save_settings(updates):
    with settings_lock:
        current = load_settings()
        for k, v in updates.items():
            if k in SETTINGS_KEYS:
                current[k] = v
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(current, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
        return current

BITRATES = ["500k", "1M", "2M", "4M"]
RESOLUTIONS = [
    ("1920x1080", "1920x1080"),
    ("1600x1200", "1600x1200"),
    ("1360x768", "1360x768"),
    ("1280x1024", "1280x1024"),
    ("1280x960", "1280x960"),
    ("1280x720", "1280x720"),
    ("1024x768", "1024x768"),
    ("800x600", "800x600"),
    ("720x576", "720x576"),
    ("720x480", "720x480"),
    ("640x480", "640x480"),
]
DEFAULT_BITRATE = "2M"
DEFAULT_RESOLUTION = "1280x720"
FORMATS = [
    {"value": "mp4", "label": "MP4 (H.264)"},
    {"value": "avi", "label": "AVI (DivX)"},
]
DEFAULT_FORMAT = "mp4"
DEFAULT_INPUT_FORMAT = "mjpeg"
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
DEFAULT_PRESET = "medium"

def get_audio_devices():
    try:
        result = subprocess.run(['arecord', '-L'], capture_output=True, text=True, timeout=10)
        print(f"Audio command return code: {result.returncode}")
        print(f"Audio command stdout: {result.stdout}")
        print(f"Audio command stderr: {result.stderr}")
        if result.returncode != 0:
            return []
        
        devices = []
        current_device = None
        
        for line in result.stdout.split('\n'):
            line = line.strip()
            if not line:
                continue
                
            if not line.startswith(' ') and not line.startswith('\t'):
                # This is a device name
                current_device = line
                # Only add hw: devices (main hardware devices)
                if current_device and current_device != 'null' and current_device.startswith('hw:'):
                    devices.append({
                        "value": current_device,
                        "label": current_device
                    })
            elif line.startswith(' ') or line.startswith('\t'):
                # This is a description line
                if current_device and current_device != 'null' and current_device.startswith('hw:'):
                    # Update the label with the description
                    for device in devices:
                        if device["value"] == current_device:
                            device["label"] = f"{current_device} - {line.strip()}"
                            break
        
        print(f"Found audio devices: {devices}")
        return devices
    except Exception as e:
        print(f"Error getting audio devices: {e}")
        return []

def get_video_devices():
    try:
        result = subprocess.run(['v4l2-ctl', '--list-devices'], capture_output=True, text=True, timeout=10)
        print(f"Video command return code: {result.returncode}")
        print(f"Video command stdout: {result.stdout}")
        print(f"Video command stderr: {result.stderr}")
        if result.returncode != 0:
            return []
        
        devices = []
        current_device = None
        
        for line in result.stdout.split('\n'):
            original_line = line
            line = line.strip()
            if not line:
                continue
                
            if not original_line.startswith('\t') and not original_line.startswith(' '):
                # This is a device name (remove trailing colon)
                current_device = line.rstrip(':')
            elif original_line.startswith('\t') or original_line.startswith(' '):
                # This is a device path
                if current_device and line.startswith('/dev/video'):
                    devices.append({
                        "value": line,
                        "label": f"{current_device} - {line}"
                    })
        
        print(f"Found video devices: {devices}")
        return devices
    except Exception as e:
        print(f"Error getting video devices: {e}")
        return []

def get_usb_devices():
    try:
        result = subprocess.run(['usbreset'], capture_output=True, text=True, timeout=10)
        print(f"USB command return code: {result.returncode}")
        print(f"USB command stdout: {result.stdout}")
        print(f"USB command stderr: {result.stderr}")
        
        devices = []
        in_devices_section = False
        
        for line in result.stdout.split('\n'):
            line = line.strip()
            if not line:
                continue
                
            if line == "Devices:":
                in_devices_section = True
                continue
                
            if in_devices_section and line.startswith("Number"):
                parts = line.split()
                if len(parts) >= 4:
                    device_id = None
                    device_name = "USB Device"
                    
                    for i, part in enumerate(parts):
                        if part == "ID" and i + 1 < len(parts):
                            device_id = parts[i + 1]
                            if i + 2 < len(parts):
                                device_name = " ".join(parts[i + 2:])
                            break
                    
                    if device_id:
                        devices.append({
                            "value": device_id,
                            "label": f"{device_name} ({device_id})"
                        })
        
        print(f"Found USB devices: {devices}")
        return devices
    except Exception as e:
        print(f"Error getting USB devices: {e}")
        return []

FFMPEG_CMD_TEMPLATES = {
    "mp4": (
        "/usr/bin/ffmpeg -y "
        "-f alsa -thread_queue_size 4096 -i {audio_device} "
        "-f v4l2 -input_format {input_format} -framerate 24 -video_size {resolution} -i {video_device} "
        "-b:v {bitrate} -b:a 192k -c:v libx264 -preset {preset} -c:a aac -pix_fmt yuv420p {output_file}"
    ),
    "avi": (
        "/usr/bin/ffmpeg -y "
        "-f alsa -thread_queue_size 4096 -i {audio_device} "
        "-f v4l2 -input_format {input_format} -framerate 24 -video_size {resolution} -i {video_device} "
        "-b:v {bitrate} -b:a 192k -c:v mpeg4 -vtag DX50 -c:a libmp3lame {output_file}"
    ),
}

os.makedirs(RECORDINGS_DIR, exist_ok=True)

ffmpeg_process = None
ffmpeg_thread = None
ffmpeg_log_lines = deque(maxlen=2000)
ffmpeg_log_queue = queue.Queue()
ws_connections = set()
shutdown_event = threading.Event()

preview_process = None
preview_thread = None
preview_latest_frame = None
preview_frame_lock = threading.Lock()

_state = {"recording": False, "previewing": False}
_state_version = 0
_state_lock = threading.Lock()

def get_state():
    with _state_lock:
        return dict(_state), _state_version

def update_state(**kwargs):
    global _state_version
    with _state_lock:
        _state.update(kwargs)
        _state_version += 1

# --- FFMPEG Process Management ---
def get_output_filename(format=DEFAULT_FORMAT):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = ".mp4" if format == "mp4" else ".avi"
    return os.path.join(RECORDINGS_DIR, f"output-{ts}{ext}")

def is_recording():
    global ffmpeg_process
    return ffmpeg_process is not None and ffmpeg_process.poll() is None

def is_previewing():
    global preview_process
    return preview_process is not None and preview_process.poll() is None

def ffmpeg_worker(cmd):
    global ffmpeg_process, ffmpeg_log_lines
    ffmpeg_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    ffmpeg_log_lines.clear()
    
    def read_output(pipe, is_stderr=False):
        try:
            for line in pipe:
                if shutdown_event.is_set():
                    break
                log_entry = {"type": "stderr" if is_stderr else "stdout", "data": line}
                ffmpeg_log_lines.append(log_entry)
                ffmpeg_log_queue.put(log_entry)
        except:
            pass
    
    stdout_thread = threading.Thread(target=read_output, args=(ffmpeg_process.stdout, False), daemon=True)
    stderr_thread = threading.Thread(target=read_output, args=(ffmpeg_process.stderr, True), daemon=True)
    
    stdout_thread.start()
    stderr_thread.start()
    
    try:
        ffmpeg_process.wait()
    finally:
        ffmpeg_process = None
        update_state(recording=False)

def preview_worker(cmd):
    global preview_process, preview_latest_frame
    preview_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    buf = b''
    try:
        while True:
            chunk = preview_process.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b'\xff\xd8\xff')
                if start == -1:
                    buf = b''
                    break
                end = buf.find(b'\xff\xd9', start + 3)
                if end == -1:
                    if start > 0:
                        buf = buf[start:]
                    break
                with preview_frame_lock:
                    preview_latest_frame = buf[start:end + 2]
                buf = buf[end + 2:]
    except Exception:
        pass
    finally:
        preview_process = None
        with preview_frame_lock:
            preview_latest_frame = None
        update_state(previewing=False)

def build_ffmpeg_cmd(bitrate, output_file, resolution, audio_device, video_device, format=DEFAULT_FORMAT, input_format=DEFAULT_INPUT_FORMAT, preset=DEFAULT_PRESET):
    template = FFMPEG_CMD_TEMPLATES[format]
    return template.format(bitrate=bitrate, output_file=output_file, resolution=resolution, audio_device=audio_device, video_device=video_device, input_format=input_format, preset=preset).split()

# --- API Endpoints ---
@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE

@app.get("/bitrates")
def get_bitrates():
    return {"bitrates": BITRATES, "default": DEFAULT_BITRATE}

@app.get("/resolutions")
def get_resolutions():
    return {"resolutions": [{"value": r[0], "label": r[1]} for r in RESOLUTIONS], "default": DEFAULT_RESOLUTION}

@app.get("/formats")
def get_formats():
    return {"formats": FORMATS, "default": DEFAULT_FORMAT}

@app.get("/presets")
def get_presets():
    return {"presets": PRESETS, "default": DEFAULT_PRESET}

@app.get("/audio-devices")
def get_audio_devices_endpoint():
    devices = get_audio_devices()
    if devices:
        devices[0]["default"] = True
    return {"audio_devices": devices}

@app.get("/video-devices")
def get_video_devices_endpoint():
    devices = get_video_devices()
    if devices:
        devices[0]["default"] = True
    return {"video_devices": devices}

def get_video_formats(device_path):
    try:
        # Ask ffmpeg to list formats directly for the device. It writes this to stderr and exits with an error code.
        result = subprocess.run(['/usr/bin/ffmpeg', '-f', 'v4l2', '-list_formats', 'all', '-i', device_path], capture_output=True, text=True, timeout=10)
        output = result.stderr
        
        formats_set = set()
        formats = []
        
        # Example output lines:
        # [video4linux2,v4l2 @ ...] Raw       :     yuyv422 :           YUYV 4:2:2 : 640x480 ...
        # [video4linux2,v4l2 @ ...] Compressed:       mjpeg :          Motion-JPEG : 640x480 ...
        for line in output.split('\n'):
            match = re.search(r"(?:Raw|Compressed)\s*:\s*([^:]+)\s*:\s*([^:]+)", line)
            if match:
                value = match.group(1).strip()
                description = match.group(2).strip()
                
                if value and value not in formats_set:
                    formats_set.add(value)
                    formats.append({"value": value, "label": f"{value.upper()} ({description})"})
                    
        print(f"Found video formats using ffmpeg for {device_path}: {formats}")
        return formats
    except Exception as e:
        print(f"Error getting video formats for {device_path}: {e}")
        return []

@app.get("/video-formats")
def get_video_formats_endpoint(device: str):
    if not device:
        return {"video_formats": []}
    formats = get_video_formats(device)
    if formats:
        formats[0]["default"] = True
    return {"video_formats": formats}

@app.get("/usb-devices")
def get_usb_devices_endpoint():
    devices = get_usb_devices()
    return {"usb_devices": devices}

@app.post("/reset-usb/{device_id}")
def reset_usb_device(device_id: str):
    try:
        result = subprocess.run(['usbreset', device_id], capture_output=True, text=True, timeout=10)
        print(f"USB reset command return code: {result.returncode}")
        print(f"USB reset command stdout: {result.stdout}")
        print(f"USB reset command stderr: {result.stderr}")
        
        if result.returncode == 0:
            return {"success": True, "message": f"USB device {device_id} reset successfully"}
        else:
            return JSONResponse({"error": f"Failed to reset USB device {device_id}: {result.stderr}"}, status_code=400)
    except Exception as e:
        print(f"Error resetting USB device {device_id}: {e}")
        return JSONResponse({"error": f"Error resetting USB device {device_id}: {str(e)}"}, status_code=500)

@app.get("/settings")
def get_settings():
    return load_settings()

@app.post("/settings")
async def post_settings(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)
    unknown = [k for k in body if k not in SETTINGS_KEYS]
    if unknown:
        return JSONResponse({"error": f"Unknown settings keys: {unknown}"}, status_code=400)
    return save_settings(body)

@app.post("/start")
def start_recording(bitrate: str = DEFAULT_BITRATE, resolution: str = DEFAULT_RESOLUTION, audio_device: str = None, video_device: str = None, format: str = DEFAULT_FORMAT, input_format: str = DEFAULT_INPUT_FORMAT, preset: str = DEFAULT_PRESET):
    global ffmpeg_thread
    if is_recording():
        return JSONResponse({"error": "Already recording"}, status_code=400)
    if is_previewing():
        return JSONResponse({"error": "Stop preview before recording"}, status_code=400)
    if bitrate not in BITRATES:
        return JSONResponse({"error": "Invalid bitrate"}, status_code=400)
    if resolution not in [r[0] for r in RESOLUTIONS]:
        return JSONResponse({"error": "Invalid resolution"}, status_code=400)
    if format not in [f["value"] for f in FORMATS]:
        return JSONResponse({"error": "Invalid format"}, status_code=400)
    if preset not in PRESETS:
        return JSONResponse({"error": "Invalid preset"}, status_code=400)
    if not audio_device or audio_device not in [d["value"] for d in get_audio_devices()]:
        return JSONResponse({"error": "Valid audio device is required"}, status_code=400)
    if not video_device or video_device not in [d["value"] for d in get_video_devices()]:
        return JSONResponse({"error": "Valid video device is required"}, status_code=400)
    output_file = get_output_filename(format)
    cmd = build_ffmpeg_cmd(bitrate, output_file, resolution, audio_device, video_device, format, input_format, preset)
    ffmpeg_thread = threading.Thread(target=ffmpeg_worker, args=(cmd,), daemon=True)
    ffmpeg_thread.start()
    update_state(recording=True)
    return {"started": True, "output": os.path.basename(output_file)}

@app.post("/stop")
def stop_recording():
    global ffmpeg_process
    if not is_recording():
        return JSONResponse({"error": "Not recording"}, status_code=400)
    ffmpeg_process.send_signal(signal.SIGINT)
    return {"stopping": True}

@app.post("/preview/start")
def start_preview(video_device: str = None, input_format: str = DEFAULT_INPUT_FORMAT):
    global preview_thread
    if is_recording():
        return JSONResponse({"error": "Cannot preview while recording"}, status_code=400)
    if is_previewing():
        return JSONResponse({"error": "Already previewing"}, status_code=400)
    if not video_device:
        return JSONResponse({"error": "Video device required"}, status_code=400)
    cmd = [
        '/usr/bin/ffmpeg', '-f', 'v4l2', '-input_format', input_format,
        '-i', video_device, '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', '5', 'pipe:1'
    ]
    preview_thread = threading.Thread(target=preview_worker, args=(cmd,), daemon=True)
    preview_thread.start()
    update_state(previewing=True)
    return {"started": True}

@app.post("/preview/stop")
def stop_preview():
    global preview_process
    if not is_previewing():
        return JSONResponse({"error": "Not previewing"}, status_code=400)
    preview_process.send_signal(signal.SIGINT)
    return {"stopping": True}

@app.get("/preview/stream")
async def preview_stream():
    if not is_previewing():
        return JSONResponse({"error": "Not previewing"}, status_code=400)

    async def generate():
        try:
            while is_previewing():
                with preview_frame_lock:
                    frame = preview_latest_frame
                if frame:
                    yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
                await asyncio.sleep(0.033)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(generate(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/logs")
def get_logs():
    return {"logs": list(ffmpeg_log_lines)[-200:]}  # last 200 lines

@app.websocket("/ws/logs")
async def websocket_logs(ws: WebSocket):
    await ws.accept()
    ws_connections.add(ws)
    last_state_version = -1
    try:
        state, version = get_state()
        await ws.send_json({"type": "state", **state})
        last_state_version = version

        for log_entry in list(ffmpeg_log_lines)[-200:]:
            await ws.send_json(log_entry)

        while not shutdown_event.is_set():
            state, version = get_state()
            if version != last_state_version:
                await ws.send_json({"type": "state", **state})
                last_state_version = version

            try:
                log_entry = ffmpeg_log_queue.get_nowait()
                await ws.send_json(log_entry)
            except queue.Empty:
                await asyncio.sleep(0.1)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        ws_connections.discard(ws)

@app.get("/files")
def list_files():
    files = sorted([os.path.join(RECORDINGS_DIR, f) for f in os.listdir(RECORDINGS_DIR) if os.path.isfile(os.path.join(RECORDINGS_DIR, f))], reverse=True)
    return [{
        "name": os.path.basename(f),
        "size": os.path.getsize(f),
        "mtime": os.path.getmtime(f)
    } for f in files]

@app.get("/files/{filename}")
def download_file(filename: str):
    file_path = os.path.join(RECORDINGS_DIR, filename)
    if not os.path.exists(file_path):
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(file_path, filename=filename)

@app.delete("/files/{filename}")
def delete_file(filename: str):
    file_path = os.path.join(RECORDINGS_DIR, filename)
    if not os.path.exists(file_path):
        return JSONResponse({"error": "File not found"}, status_code=404)
    os.remove(file_path)
    return {"deleted": True}

@app.get("/thumbnails/{filename}")
def get_thumbnail(filename: str, request: Request):
    file_path = os.path.join(RECORDINGS_DIR, filename)
    if not os.path.exists(file_path):
        return JSONResponse({"error": "File not found"}, status_code=404)
    mtime = os.path.getmtime(file_path)
    last_modified = datetime.utcfromtimestamp(mtime).strftime("%a, %d %b %Y %H:%M:%S GMT")
    if request.headers.get("if-modified-since") == last_modified:
        return Response(status_code=304)
    try:
        result = subprocess.run(
            ['/usr/bin/ffmpeg', '-ss', '1', '-i', file_path, '-vframes', '1', '-f', 'image2', '-vcodec', 'mjpeg', 'pipe:1'],
            capture_output=True, timeout=15
        )
        if result.returncode != 0 or not result.stdout:
            return JSONResponse({"error": "Could not generate thumbnail"}, status_code=500)
        return Response(content=result.stdout, media_type="image/jpeg", headers={
            "Cache-Control": "max-age=86400",
            "Last-Modified": last_modified,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)



# --- Simple HTML/JS Frontend ---
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>FFMPEG Recorder</title>
    <style>
        body { font-family: sans-serif; margin: 2em; }
        .controls { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 1em; margin-bottom: 1em; }
        .field { display: flex; flex-direction: column; gap: 0.25em; }
        .field label { font-size: 0.85em; font-weight: bold; }
        .field select { width: 100%; }
        .usb-row { display: flex; align-items: center; gap: 1em; margin-bottom: 1em; }
        #advanced { margin: 0.5em 0 1em; }
        #advanced > summary { cursor: pointer; font-weight: bold; padding: 0.4em 0; user-select: none; }
        #advanced[open] > summary { margin-bottom: 0.5em; }
        #logs { background: #111; color: #0f0; padding: 1em; height: 300px; overflow-y: scroll; font-family: monospace; }
        .file-row { display: flex; align-items: center; gap: 1em; margin-bottom: 0.5em; }
        .file-thumb { height: 60px; width: auto; cursor: pointer; border-radius: 2px; background: #222; }
        #videoModal { padding: 0; border: none; border-radius: 4px; background: #000; max-width: 90vw; }
        #videoModal::backdrop { background: rgba(0,0,0,0.75); }
        .modal-close { display: block; margin: 0.4em auto; background: #333; color: #fff; border: none; padding: 0.4em 1.5em; cursor: pointer; border-radius: 3px; }
        #previewSection { margin-top: 1em; }
        #previewImg { display: block; max-width: 100%; height: auto; border-radius: 4px; }
    </style>
</head>
<body>
    <h1>FFMPEG Recorder</h1>

    <div class="controls">
        <div class="field"><label for="bitrate">Bitrate</label><select id="bitrate"></select></div>
        <div class="field"><label for="format">Output Format</label><select id="format"></select></div>
        <div class="field"><label for="resolution">Resolution</label><select id="resolution"></select></div>
    </div>

    <details id="advanced">
        <summary>Advanced</summary>
        <div class="controls">
            <div class="field"><label for="preset">Preset (H.264)</label><select id="preset"></select></div>
            <div class="field"><label for="input_format">Input Format</label><select id="input_format"></select></div>
            <div class="field"><label for="video_device">Video Device</label><select id="video_device"></select></div>
            <div class="field"><label for="audio_device">Audio Device</label><select id="audio_device"></select></div>
        </div>
    </details>

    <div style="margin-bottom: 1.5em;">
        <button id="previewToggleBtn">&#128247; Start Preview</button>
        <button id="recordToggleBtn" disabled>&#128308; Start Recording</button>
        <span id="status"></span>
    </div>

    <div id="previewSection" style="display:none;">
        <img id="previewImg">
    </div>

    <div class="usb-row" style="margin-top: 1.5em;">
        <label for="usb_device">USB Device:</label>
        <select id="usb_device"></select>
        <button id="resetUsbBtn">Reset USB Device</button>
    </div>

    <h2>Logs</h2>
    <div id="logs"></div>
    <h2>Recorded Files</h2>
    <div id="files"></div>
    <dialog id="videoModal">
        <video id="modalVideo" controls autoplay style="display:block;max-width:85vw;max-height:80vh;"></video>
        <button class="modal-close" onclick="closeVideoModal()">Close</button>
    </dialog>
    <script>
        let ws;
        let savedSettings = {};

        function pickValue(items, getValue, saved, fallbackPredicate) {
            if (saved !== undefined && saved !== null && items.some(i => getValue(i) === saved)) return saved;
            const fb = items.find(fallbackPredicate);
            if (fb) return getValue(fb);
            return items.length ? getValue(items[0]) : null;
        }
        function populateSelect(sel, items, getValue, getLabel, selectedValue) {
            sel.innerHTML = '';
            items.forEach(i => {
                const o = document.createElement('option');
                o.value = getValue(i); o.text = getLabel(i);
                if (getValue(i) === selectedValue) o.selected = true;
                sel.appendChild(o);
            });
        }
        function saveSetting(key, value) {
            fetch('/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({[key]: value})
            }).catch(e => console.error('Failed to save setting', key, e));
        }

        function fetchBitrates(saved) {
            return fetch('/bitrates').then(r => r.json()).then(d => {
                const items = d.bitrates;
                const selected = pickValue(items, x => x, saved, x => x === d.default);
                const sel = document.getElementById('bitrate');
                populateSelect(sel, items, x => x, x => x, selected);
            });
        }
        function fetchResolutions(saved) {
            return fetch('/resolutions').then(r => r.json()).then(d => {
                const items = d.resolutions;
                const selected = pickValue(items, x => x.value, saved, x => x.value === d.default);
                populateSelect(document.getElementById('resolution'), items, x => x.value, x => x.label, selected);
            });
        }
        function fetchFormats(saved) {
            return fetch('/formats').then(r => r.json()).then(d => {
                const items = d.formats;
                const selected = pickValue(items, x => x.value, saved, x => x.value === d.default);
                populateSelect(document.getElementById('format'), items, x => x.value, x => x.label, selected);
            });
        }
        function fetchPresets(saved) {
            return fetch('/presets').then(r => r.json()).then(d => {
                const items = d.presets;
                const selected = pickValue(items, x => x, saved, x => x === d.default);
                populateSelect(document.getElementById('preset'), items, x => x, x => x, selected);
            });
        }
        function fetchAudioDevices(saved) {
            return fetch('/audio-devices').then(r => r.json()).then(d => {
                const items = d.audio_devices;
                const selected = pickValue(items, x => x.value, saved, x => x.default);
                populateSelect(document.getElementById('audio_device'), items, x => x.value, x => x.label, selected);
            });
        }
        function fetchVideoDevices(savedDevice, savedInputFormat) {
            return fetch('/video-devices').then(r => r.json()).then(d => {
                const items = d.video_devices;
                const selected = pickValue(items, x => x.value, savedDevice, x => x.default);
                populateSelect(document.getElementById('video_device'), items, x => x.value, x => x.label, selected);
                return fetchVideoFormats(savedInputFormat);
            });
        }
        function fetchVideoFormats(saved) {
            const device = document.getElementById('video_device').value;
            const sel = document.getElementById('input_format');
            if (!device) { sel.innerHTML = ''; return Promise.resolve(); }
            return fetch('/video-formats?device=' + encodeURIComponent(device)).then(r => r.json()).then(d => {
                if (d.video_formats && d.video_formats.length > 0) {
                    const items = d.video_formats;
                    const selected = pickValue(items, x => x.value, saved, x => x.default);
                    populateSelect(sel, items, x => x.value, x => x.label, selected);
                } else {
                    sel.innerHTML = '<option value="mjpeg">MJPEG (Fallback)</option>';
                }
            }).catch(() => {
                sel.innerHTML = '<option value="mjpeg">MJPEG (Fallback)</option>';
            });
        }
        function fetchUsbDevices() {
            return fetch('/usb-devices').then(r => r.json()).then(d => {
                const sel = document.getElementById('usb_device');
                sel.innerHTML = '';
                d.usb_devices.forEach(dev => {
                    const o = document.createElement('option');
                    o.value = dev.value; o.text = dev.label;
                    sel.appendChild(o);
                });
            });
        }
        function resetUsbDevice() {
            const usb_device = document.getElementById('usb_device').value;
            if (!usb_device) {
                alert('Please select a USB device to reset');
                return;
            }
            fetch('/reset-usb/' + encodeURIComponent(usb_device), {method: 'POST'})
                .then(r => r.json()).then(d => {
                    if (d.error) alert(d.error);
                    else alert(d.message || 'USB device reset successfully');
                });
        }
        function applyState(state) {
            const previewing = !!state.previewing;
            const recording = !!state.recording;
            const toggleBtn = document.getElementById('previewToggleBtn');
            toggleBtn.textContent = previewing ? '⏹ Stop Preview' : '📷 Start Preview';
            toggleBtn.disabled = recording;
            toggleBtn.onclick = previewing ? stopPreview : startPreview;
            const recBtn = document.getElementById('recordToggleBtn');
            recBtn.textContent = recording ? '⏹ Stop Recording' : '🔴 Start Recording';
            recBtn.disabled = previewing;
            recBtn.onclick = recording ? stopRecording : startRecording;
            document.getElementById('status').innerText = recording ? 'Recording...' : previewing ? 'Previewing...' : 'Idle';
            const section = document.getElementById('previewSection');
            const img = document.getElementById('previewImg');
            if (previewing) {
                if (!img.src.endsWith('/preview/stream')) img.src = '/preview/stream';
                section.style.display = 'block';
            } else {
                section.style.display = 'none';
                img.src = '';
            }
        }
        function startRecording() {
            const bitrate = document.getElementById('bitrate').value;
            const resolution = document.getElementById('resolution').value;
            const audio_device = document.getElementById('audio_device').value;
            const video_device = document.getElementById('video_device').value;
            const format = document.getElementById('format').value;
            const input_format = document.getElementById('input_format').value;
            const preset = document.getElementById('preset').value;
            const params = new URLSearchParams({bitrate, resolution, audio_device, video_device, format, input_format, preset});
            fetch('/start?' + params.toString(), {method: 'POST'})
                .then(r => r.json()).then(d => { if (d.error) alert(d.error); });
        }
        function stopRecording() {
            fetch('/stop', {method: 'POST'})
                .then(r => r.json()).then(d => { if (d.error) alert(d.error); });
        }
        function startPreview() {
            const video_device = document.getElementById('video_device').value;
            const input_format = document.getElementById('input_format').value;
            const params = new URLSearchParams({video_device, input_format});
            fetch('/preview/start?' + params.toString(), {method: 'POST'})
                .then(r => r.json()).then(d => {
                    if (d.error && d.error !== 'Already previewing') alert(d.error);
                });
        }
        function stopPreview() {
            fetch('/preview/stop', {method: 'POST'})
                .then(r => r.json()).then(d => { if (d.error) alert(d.error); });
        }
        function connectLogs() {
            const protocol = location.protocol === 'https:' ? 'wss://' : 'ws://';
            if (ws) ws.close();
            ws = new WebSocket(protocol + location.host + '/ws/logs');
            ws.onmessage = e => {
                const msg = JSON.parse(e.data);
                if (msg.type === 'state') {
                    applyState(msg);
                    return;
                }
                const logs = document.getElementById('logs');
                const escapedData = msg.data.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                logs.innerHTML += `<span style=\"color: #fff;\">${escapedData.replace(/\\n/g, '<br>')}</span><br>`;
                logs.scrollTop = logs.scrollHeight;
            };
            ws.onclose = () => setTimeout(connectLogs, 2000);
        }
        const BROWSER_PLAYABLE = new Set(['mp4', 'webm', 'ogg', 'mov']);
        function fileExt(name) { return name.split('.').pop().toLowerCase(); }

        function loadFiles() {
            fetch('/files').then(r => r.json()).then(files => {
                const filesDiv = document.getElementById('files');
                filesDiv.innerHTML = '';
                files.forEach(f => {
                    const row = document.createElement('div');
                    row.className = 'file-row';
                    const canPlay = BROWSER_PLAYABLE.has(fileExt(f.name));
                    row.innerHTML =
                        `<img class="file-thumb" src="/thumbnails/${encodeURIComponent(f.name)}" alt="" loading="lazy"` +
                        (canPlay ? ` onclick="openVideoModal('${f.name}')" title="Play"` : '') + `>` +
                        `<span>${f.name}</span>` +
                        `<span>${(f.size/1024/1024).toFixed(2)} MB</span>` +
                        `<span>${new Date(f.mtime*1000).toLocaleString()}</span>` +
                        `<a href="/files/${encodeURIComponent(f.name)}" download>Download</a>` +
                        `<button onclick="deleteFile('${f.name}')">Delete</button>`;
                    filesDiv.appendChild(row);
                });
            });
        }
        function deleteFile(name) {
            if (!confirm(`Delete ${name}?`)) return;
            fetch('/files/' + encodeURIComponent(name), {method: 'DELETE'})
                .then(r => r.json()).then(d => { if (d.deleted) loadFiles(); });
        }
        function openVideoModal(name) {
            const video = document.getElementById('modalVideo');
            video.src = '/files/' + encodeURIComponent(name);
            document.getElementById('videoModal').showModal();
        }
        function closeVideoModal() {
            const modal = document.getElementById('videoModal');
            const video = document.getElementById('modalVideo');
            modal.close();
            video.pause();
            video.src = '';
        }
        document.addEventListener('DOMContentLoaded', () => {
            document.getElementById('videoModal').addEventListener('click', e => {
                if (e.target === e.currentTarget) closeVideoModal();
            });
        });

        function wirePersistence() {
            const map = {
                bitrate: 'bitrate',
                format: 'format',
                resolution: 'resolution',
                preset: 'preset',
                input_format: 'input_format',
                video_device: 'video_device',
                audio_device: 'audio_device',
            };
            Object.entries(map).forEach(([key, id]) => {
                document.getElementById(id).addEventListener('change', e => saveSetting(key, e.target.value));
            });
            document.getElementById('video_device').addEventListener('change', () => fetchVideoFormats());
            document.getElementById('advanced').addEventListener('toggle', e => saveSetting('advanced_open', e.target.open));
        }

        async function init() {
            document.getElementById('resetUsbBtn').onclick = resetUsbDevice;

            try {
                savedSettings = await fetch('/settings').then(r => r.json());
            } catch (e) {
                savedSettings = {};
            }
            document.getElementById('advanced').open = !!savedSettings.advanced_open;

            await Promise.all([
                fetchBitrates(savedSettings.bitrate),
                fetchResolutions(savedSettings.resolution),
                fetchFormats(savedSettings.format),
                fetchPresets(savedSettings.preset),
                fetchAudioDevices(savedSettings.audio_device),
                fetchUsbDevices(),
                fetchVideoDevices(savedSettings.video_device, savedSettings.input_format),
            ]);

            wirePersistence();
            connectLogs();
            loadFiles();
            setInterval(loadFiles, 5000);

        }
        init();
    </script>
</body>
</html>
"""