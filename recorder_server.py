import os
import signal
import subprocess
import threading
import time
import re
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
import glob
import queue
import asyncio
from collections import deque

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
                # Add the device if it's not null
                if current_device and current_device != 'null':
                    devices.append({
                        "value": current_device,
                        "label": current_device
                    })
            elif line.startswith(' ') or line.startswith('\t'):
                # This is a description line
                if current_device and current_device != 'null':
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
            line = line.strip()
            if not line:
                continue
                
            if not line.startswith('\t'):
                # This is a device name
                current_device = line
            elif line.startswith('\t'):
                # This is a device path
                if current_device and line.startswith('/dev/video'):
                    devices.append({
                        "value": line.strip(),
                        "label": f"{current_device} - {line.strip()}"
                    })
        
        print(f"Found video devices: {devices}")
        return devices
    except Exception as e:
        print(f"Error getting video devices: {e}")
        return []

AUDIO_DEVICES = get_audio_devices()
VIDEO_DEVICES = get_video_devices()

FFMPEG_CMD_TEMPLATES = {
    "mp4": (
        "/usr/bin/ffmpeg -y "
        "-f alsa -i {audio_device} "
        "-f v4l2 -input_format mjpeg -framerate 24 -video_size {resolution} -i {video_device} "
        "-b:v {bitrate} -b:a 192k -c:v libx264 -c:a aac -pix_fmt yuv420p {output_file}"
    ),
    "avi": (
        "/usr/bin/ffmpeg -y "
        "-f alsa -i {audio_device} "
        "-f v4l2 -input_format mjpeg -framerate 24 -video_size {resolution} -i {video_device} "
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

# --- FFMPEG Process Management ---
def get_output_filename(format=DEFAULT_FORMAT):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = ".mp4" if format == "mp4" else ".avi"
    return os.path.join(RECORDINGS_DIR, f"output-{ts}{ext}")

def is_recording():
    global ffmpeg_process
    return ffmpeg_process is not None and ffmpeg_process.poll() is None

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

def build_ffmpeg_cmd(bitrate, output_file, resolution, audio_device, video_device, format=DEFAULT_FORMAT):
    template = FFMPEG_CMD_TEMPLATES[format]
    return template.format(bitrate=bitrate, output_file=output_file, resolution=resolution, audio_device=audio_device, video_device=video_device).split()

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

@app.get("/audio-devices")
def get_audio_devices_endpoint():
    return {"audio_devices": AUDIO_DEVICES}

@app.get("/video-devices")
def get_video_devices_endpoint():
    return {"video_devices": VIDEO_DEVICES}

@app.get("/status")
def status():
    return {"recording": is_recording()}

@app.post("/start")
def start_recording(bitrate: str = DEFAULT_BITRATE, resolution: str = DEFAULT_RESOLUTION, audio_device: str = None, video_device: str = None, format: str = DEFAULT_FORMAT):
    global ffmpeg_thread
    if is_recording():
        return JSONResponse({"error": "Already recording"}, status_code=400)
    if bitrate not in BITRATES:
        return JSONResponse({"error": "Invalid bitrate"}, status_code=400)
    if resolution not in [r[0] for r in RESOLUTIONS]:
        return JSONResponse({"error": "Invalid resolution"}, status_code=400)
    if format not in [f["value"] for f in FORMATS]:
        return JSONResponse({"error": "Invalid format"}, status_code=400)
    if not audio_device or audio_device not in [d["value"] for d in AUDIO_DEVICES]:
        return JSONResponse({"error": "Valid audio device is required"}, status_code=400)
    if not video_device or video_device not in [d["value"] for d in VIDEO_DEVICES]:
        return JSONResponse({"error": "Valid video device is required"}, status_code=400)
    output_file = get_output_filename(format)
    cmd = build_ffmpeg_cmd(bitrate, output_file, resolution, audio_device, video_device, format)
    ffmpeg_thread = threading.Thread(target=ffmpeg_worker, args=(cmd,), daemon=True)
    ffmpeg_thread.start()
    return {"started": True, "output": os.path.basename(output_file)}

@app.post("/stop")
def stop_recording():
    global ffmpeg_process
    if not is_recording():
        return JSONResponse({"error": "Not recording"}, status_code=400)
    ffmpeg_process.send_signal(signal.SIGINT)
    return {"stopping": True}

@app.get("/logs")
def get_logs():
    return {"logs": list(ffmpeg_log_lines)[-200:]}  # last 200 lines

@app.websocket("/ws/logs")
async def websocket_logs(ws: WebSocket):
    await ws.accept()
    ws_connections.add(ws)
    try:
        # Send last 200 lines on connect
        for log_entry in list(ffmpeg_log_lines)[-200:]:
            await ws.send_json(log_entry)
        while not shutdown_event.is_set():
            sent = False
            try:
                # Drain all available log lines
                while True:
                    log_entry = ffmpeg_log_queue.get_nowait()
                    await ws.send_json(log_entry)
                    sent = True
            except queue.Empty:
                pass
            if not sent:
                await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        pass
    finally:
        ws_connections.discard(ws)
        # Ensure websocket is closed on shutdown
        if shutdown_event.is_set():
            try:
                await ws.close()
            except Exception:
                pass

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

# --- Shutdown Handler ---
@app.on_event("shutdown")
async def shutdown_event_handler():
    global ffmpeg_process, shutdown_event
    shutdown_event.set()
    # Close all websocket connections
    for ws in list(ws_connections):
        try:
            await ws.close()
        except Exception:
            pass
    if ffmpeg_process and ffmpeg_process.poll() is None:
        try:
            ffmpeg_process.terminate()
            ffmpeg_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ffmpeg_process.kill()
        except:
            pass

# --- Simple HTML/JS Frontend ---
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>FFMPEG Recorder</title>
    <style>
        body { font-family: sans-serif; margin: 2em; }
        #logs { background: #111; color: #0f0; padding: 1em; height: 300px; overflow-y: scroll; font-family: monospace; }
        #files { margin-top: 2em; }
        .file-row { display: flex; align-items: center; gap: 1em; }
        button { margin-left: 1em; }
    </style>
</head>
<body>
    <h1>FFMPEG Recorder</h1>
    <div>
        <label for="bitrate">Bitrate:</label>
        <select id="bitrate"></select>
        <label for="resolution">Resolution:</label>
        <select id="resolution"></select>
        <label for="audio_device">Audio Device:</label>
        <select id="audio_device"></select>
        <label for="video_device">Video Device:</label>
        <select id="video_device"></select>
        <label for="format">Format:</label>
        <select id="format"></select>
        <button id="startBtn">Start Recording</button>
        <button id="stopBtn" disabled>Stop Recording</button>
    </div>
    <div id="status"></div>
    <h2>Logs</h2>
    <div id="logs"></div>
    <h2>Recorded Files</h2>
    <div id="files"></div>
    <script>
        let ws;
        function fetchBitrates() {
            fetch('/bitrates').then(r => r.json()).then(d => {
                let sel = document.getElementById('bitrate');
                sel.innerHTML = '';
                d.bitrates.forEach(b => {
                    let o = document.createElement('option');
                    o.value = b; o.text = b;
                    if (b === d.default) o.selected = true;
                    sel.appendChild(o);
                });
            });
        }
        function fetchResolutions() {
            fetch('/resolutions').then(r => r.json()).then(d => {
                let sel = document.getElementById('resolution');
                sel.innerHTML = '';
                d.resolutions.forEach(r => {
                    let o = document.createElement('option');
                    o.value = r.value; o.text = r.label;
                    if (r.value === d.default) o.selected = true;
                    sel.appendChild(o);
                });
            });
        }
        function fetchFormats() {
            fetch('/formats').then(r => r.json()).then(d => {
                let sel = document.getElementById('format');
                sel.innerHTML = '';
                d.formats.forEach(f => {
                    let o = document.createElement('option');
                    o.value = f.value; o.text = f.label;
                    if (f.value === d.default) o.selected = true;
                    sel.appendChild(o);
                });
            });
        }
        function fetchAudioDevices() {
            fetch('/audio-devices').then(r => r.json()).then(d => {
                let sel = document.getElementById('audio_device');
                sel.innerHTML = '<option value="">Select Audio Device</option>';
                d.audio_devices.forEach(d => {
                    let o = document.createElement('option');
                    o.value = d.value; o.text = d.label;
                    sel.appendChild(o);
                });
            });
        }
        function fetchVideoDevices() {
            fetch('/video-devices').then(r => r.json()).then(d => {
                let sel = document.getElementById('video_device');
                sel.innerHTML = '<option value="">Select Video Device</option>';
                d.video_devices.forEach(d => {
                    let o = document.createElement('option');
                    o.value = d.value; o.text = d.label;
                    sel.appendChild(o);
                });
            });
        }
        function updateStatus() {
            fetch('/status').then(r => r.json()).then(d => {
                document.getElementById('startBtn').disabled = d.recording;
                document.getElementById('stopBtn').disabled = !d.recording;
                document.getElementById('status').innerText = d.recording ? 'Recording...' : 'Idle';
            });
        }
        function startRecording() {
            let bitrate = document.getElementById('bitrate').value;
            let resolution = document.getElementById('resolution').value;
            let audio_device = document.getElementById('audio_device').value;
            let video_device = document.getElementById('video_device').value;
            let format = document.getElementById('format').value;
            let params = new URLSearchParams({bitrate, resolution, audio_device, video_device, format});
            fetch('/start?' + params.toString(), {method: 'POST'})
                .then(r => r.json()).then(d => {
                    if (d.error) alert(d.error);
                    updateStatus();
                    connectLogs();
                });
        }
        function stopRecording() {
            fetch('/stop', {method: 'POST'})
                .then(r => r.json()).then(d => {
                    if (d.error) alert(d.error);
                    updateStatus();
                });
        }
        function connectLogs() {
            let protocol = location.protocol === 'https:' ? 'wss://' : 'ws://';
            if (ws) ws.close();
            ws = new WebSocket(protocol + location.host + '/ws/logs');
            ws.onmessage = e => {
                let logs = document.getElementById('logs');
                let logEntry = JSON.parse(e.data);
                let escapedData = logEntry.data.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                logs.innerHTML += `<span style=\"color: #fff;\">${escapedData.replace(/\\n/g, '<br>')}</span><br>`;
                logs.scrollTop = logs.scrollHeight;
            };
        }
        function loadFiles() {
            fetch('/files').then(r => r.json()).then(files => {
                let filesDiv = document.getElementById('files');
                filesDiv.innerHTML = '';
                files.forEach(f => {
                    let row = document.createElement('div');
                    row.className = 'file-row';
                    row.innerHTML = `<span>${f.name}</span> <span>${(f.size/1024/1024).toFixed(2)} MB</span> <span>${new Date(f.mtime*1000).toLocaleString()}</span>` +
                        `<a href="/files/${f.name}" download>Download</a>` +
                        `<button onclick="deleteFile('${f.name}')">Delete</button>`;
                    filesDiv.appendChild(row);
                });
            });
        }
        function deleteFile(name) {
            fetch('/files/' + encodeURIComponent(name), {method: 'DELETE'})
                .then(r => r.json()).then(d => { if (d.deleted) loadFiles(); });
        }
        document.getElementById('startBtn').onclick = startRecording;
        document.getElementById('stopBtn').onclick = stopRecording;
        fetchBitrates();
        fetchResolutions();
        fetchAudioDevices();
        fetchVideoDevices();
        fetchFormats();
        updateStatus();
        loadFiles();
        connectLogs();
        setInterval(updateStatus, 2000);
        setInterval(loadFiles, 5000);
    </script>
</body>
</html>
""" 