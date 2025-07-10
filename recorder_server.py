import os
import signal
import subprocess
import threading
import time
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
    ("640x360", "360p"),
    ("1280x720", "720p"),
    ("1920x1080", "1080p"),
    ("3840x2160", "4K"),
]
DEFAULT_BITRATE = "2M"
DEFAULT_RESOLUTION = "1280x720"

FFMPEG_CMD_TEMPLATE = (
    "/usr/bin/ffmpeg -y "
    "-f alsa -i hw:0 "
    "-f v4l2 -input_format mjpeg -framerate 30 -video_size {resolution} -i /dev/video0 "
    "-b:v {bitrate} -b:a 192k -c:v libx264 -c:a aac -pix_fmt yuv420p {output_file}"
)

os.makedirs(RECORDINGS_DIR, exist_ok=True)

ffmpeg_process = None
ffmpeg_thread = None
ffmpeg_log_lines = deque(maxlen=2000)
ffmpeg_log_queue = queue.Queue()
ws_connections = set()
shutdown_event = threading.Event()

# --- FFMPEG Process Management ---
def get_output_filename():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(RECORDINGS_DIR, f"output-{ts}.mp4")

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

def build_ffmpeg_cmd(bitrate, output_file, resolution):
    return FFMPEG_CMD_TEMPLATE.format(bitrate=bitrate, output_file=output_file, resolution=resolution).split()

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

@app.get("/status")
def status():
    return {"recording": is_recording()}

@app.post("/start")
def start_recording(bitrate: str = DEFAULT_BITRATE, resolution: str = DEFAULT_RESOLUTION):
    global ffmpeg_thread
    if is_recording():
        return JSONResponse({"error": "Already recording"}, status_code=400)
    if bitrate not in BITRATES:
        return JSONResponse({"error": "Invalid bitrate"}, status_code=400)
    if resolution not in [r[0] for r in RESOLUTIONS]:
        return JSONResponse({"error": "Invalid resolution"}, status_code=400)
    output_file = get_output_filename()
    cmd = build_ffmpeg_cmd(bitrate, output_file, resolution)
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

@app.get("/files")
def list_files():
    files = sorted(glob.glob(os.path.join(RECORDINGS_DIR, "output-*.mp4")), reverse=True)
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
            let params = new URLSearchParams({bitrate, resolution});
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
        updateStatus();
        loadFiles();
        connectLogs();
        setInterval(updateStatus, 2000);
        setInterval(loadFiles, 5000);
    </script>
</body>
</html>
""" 