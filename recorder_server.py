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

FFMPEG_CMD_TEMPLATE = (
    "/usr/bin/ffmpeg -y "
    "-f alsa -i hw:0 "
    "-f v4l2 -input_format mjpeg -framerate 30 -video_size 1280x720 -i /dev/video0 "
    "-b:v {bitrate} -b:a 192k -c:v libx264 -c:a aac -pix_fmt yuv420p {output_file}"
)

os.makedirs(RECORDINGS_DIR, exist_ok=True)

ffmpeg_process = None
ffmpeg_thread = None
ffmpeg_log_lines = []
ffmpeg_log_queue = queue.Queue()
ws_connections = set()

# --- FFMPEG Process Management ---
def get_output_filename():
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(RECORDINGS_DIR, f"output-{ts}.mp4")

def is_recording():
    global ffmpeg_process
    return ffmpeg_process is not None and ffmpeg_process.poll() is None

def ffmpeg_worker(cmd):
    global ffmpeg_process, ffmpeg_log_lines
    ffmpeg_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    ffmpeg_log_lines = []
    try:
        for line in ffmpeg_process.stdout:
            ffmpeg_log_lines.append(line)
            ffmpeg_log_queue.put(line)
    finally:
        ffmpeg_process = None

def build_ffmpeg_cmd(bitrate, output_file):
    return FFMPEG_CMD_TEMPLATE.format(bitrate=bitrate, output_file=output_file).split()

# --- API Endpoints ---
@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE

@app.get("/bitrates")
def get_bitrates():
    return {"bitrates": BITRATES}

@app.get("/status")
def status():
    return {"recording": is_recording()}

@app.post("/start")
def start_recording(bitrate: str):
    global ffmpeg_thread
    if is_recording():
        return JSONResponse({"error": "Already recording"}, status_code=400)
    if bitrate not in BITRATES:
        return JSONResponse({"error": "Invalid bitrate"}, status_code=400)
    output_file = get_output_filename()
    cmd = build_ffmpeg_cmd(bitrate, output_file)
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
    return {"logs": ffmpeg_log_lines[-200:]}  # last 200 lines

@app.websocket("/ws/logs")
async def websocket_logs(ws: WebSocket):
    await ws.accept()
    ws_connections.add(ws)
    try:
        # Send last 200 lines on connect
        for line in ffmpeg_log_lines[-200:]:
            await ws.send_text(line)
        while True:
            # Send new log lines from the queue
            try:
                line = ffmpeg_log_queue.get(timeout=1)
                await ws.send_text(line)
            except queue.Empty:
                pass
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
                d.bitrates.forEach(b => {
                    let o = document.createElement('option');
                    o.value = b; o.text = b;
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
            fetch('/start?bitrate=' + encodeURIComponent(bitrate), {method: 'POST'})
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
            if (ws) ws.close();
            ws = new WebSocket('ws://' + location.host + '/ws/logs');
            ws.onmessage = e => {
                let logs = document.getElementById('logs');
                logs.textContent += e.data;
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
        updateStatus();
        loadFiles();
        connectLogs();
        setInterval(updateStatus, 2000);
        setInterval(loadFiles, 5000);
    </script>
</body>
</html>
""" 