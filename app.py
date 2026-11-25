"""Personal home dashboard for Raspberry Pi.
Run:  pip install -r requirements.txt && python app.py
Open: http://<pi-ip>:8000  (or your Tailscale address from your phone)
"""
import base64, json, os, shutil, time, threading, urllib.request
from concurrent.futures import ThreadPoolExecutor
import psutil
from flask import Flask, jsonify, request, Response, send_from_directory
from werkzeug.utils import secure_filename

APP_NAME = "Home"   # name shown under the home screen icon

# ---- edit these ----
LAT, LON = 40.71, -74.01          # for weather (Open-Meteo, no API key)
UNITS = "fahrenheit"              # or "celsius"
WEEK_START = 0                    # 0 = Sunday, 1 = Monday
UPLOAD_DIR = ""                   # blank = "uploads" folder next to app.py, or e.g. "/mnt/usb"
MAX_UPLOAD_GB = 4                 # biggest single file allowed
LINKS = [("Router", "http://192.168.1.1"),("Canvas","https://canvas.uoregon.edu") ,("Pi-hole", "http://pi.hole/admin"),
         ("Jellyfin", "http://pi.local:8096"), ("Claude", "https://claude.ai/new"),
         ("Remote Connect", "https://connect.raspberrypi.com/devices")]
# Other Pis to show in the carousel. Each one runs this same app.
# Use its Tailscale address:  ("Name", "http://100.x.x.x:8000")
PEERS = [
    # ("Garage Pi", "http://100.x.x.x:8000"),
]
# --------------------

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
lock = threading.Lock()
UPLOAD_DIR = UPLOAD_DIR or os.path.join(os.path.dirname(DATA), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
for _e in list(os.scandir(UPLOAD_DIR)):          # old flat uploads -> uploads/Unsorted
    if _e.is_file() and not _e.name.startswith("."):
        os.makedirs(os.path.join(UPLOAD_DIR, "Unsorted"), exist_ok=True)
        os.replace(_e.path, os.path.join(UPLOAD_DIR, "Unsorted", _e.name))
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(MAX_UPLOAD_GB * 1024**3)

def load():
    try:
        with open(DATA) as f: return json.load(f)
    except Exception: return {"todos": [], "notes": "", "events": {}}

def save(d):
    with open(DATA + ".tmp", "w") as f: json.dump(d, f)
    os.replace(DATA + ".tmp", DATA)

def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f: return int(f.read()) / 1000
    except Exception: return None

@app.get("/api/stats")
def stats():
    du = shutil.disk_usage("/")
    return jsonify(cpu=psutil.cpu_percent(interval=0.3), temp=cpu_temp(),
                   mem=psutil.virtual_memory().percent, disk=du.used / du.total * 100,
                   uptime=int(time.time() - psutil.boot_time()))

def fetch_peer(p):
    name, url = p
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/stats", timeout=3) as r:
            d = json.load(r)
        d.update(name=name, online=True)
    except Exception:
        d = {"name": name, "online": False}
    return d

@app.get("/api/peers")
def peers():
    if not PEERS: return jsonify([])
    with ThreadPoolExecutor(len(PEERS)) as ex:
        return jsonify(list(ex.map(fetch_peer, PEERS)))

@app.get("/api/data")
def get_data():
    return jsonify(load())

@app.post("/api/data")
def set_data():
    body = request.get_json(force=True)
    with lock:
        d = load()
        if "todos" in body: d["todos"] = body["todos"][:200]
        if "notes" in body: d["notes"] = str(body["notes"])[:20000]
        if isinstance(body.get("events"), dict):
            d["events"] = {k: v[:50] for k, v in list(body["events"].items())[:3000] if isinstance(v, list)}
        save(d)
    return jsonify(ok=True)

def clean(path):
    """'a/b' -> ['a','b']; rejects anything that isn't a plain, safe folder name."""
    parts = [p for p in (path or "").split("/") if p]
    for p in parts:
        if secure_filename(p) != p:
            raise ValueError("bad path")
    return parts

def resolve(path, create=False):
    parts = clean(path)
    full = os.path.join(UPLOAD_DIR, *parts)
    if create: os.makedirs(full, exist_ok=True)
    return full, parts

def unique(folder, name):
    base, ext = os.path.splitext(name)
    cand, n = name, 1
    while os.path.exists(os.path.join(folder, cand)):
        cand, n = f"{base}-{n}{ext}", n + 1
    return cand

@app.post("/api/device")
def device():
    body = request.get_json(force=True)
    name = secure_filename(str(body.get("name", "")).strip()) or "device"
    old = str(body.get("old", ""))
    new = os.path.join(UPLOAD_DIR, name)
    if old and old != name and "/" not in old:
        try: oldp, _ = resolve(old)
        except ValueError: oldp = None
        if oldp and os.path.isdir(oldp) and not os.path.exists(new):
            os.rename(oldp, new)
    os.makedirs(new, exist_ok=True)
    return jsonify(slug=name)

@app.get("/api/files")
def list_files():
    try: full, parts = resolve(request.args.get("path", ""))
    except ValueError: return jsonify(error="bad path"), 400
    if not os.path.isdir(full): return jsonify(error="not found"), 404
    folders, files = [], []
    for e in os.scandir(full):
        if e.name.startswith("."): continue
        st = e.stat()
        if e.is_dir():
            folders.append({"name": e.name, "count": sum(1 for _ in os.scandir(e.path))})
        elif e.is_file():
            files.append({"name": e.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    folders.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: -x["mtime"])
    return jsonify(path="/".join(parts), folders=folders, files=files,
                   free=shutil.disk_usage(UPLOAD_DIR).free)

@app.post("/api/mkdir")
def mkdir():
    body = request.get_json(force=True)
    name = secure_filename(str(body.get("name", "")).strip())
    try: full, parts = resolve(body.get("path", ""))
    except ValueError: return jsonify(error="bad path"), 400
    if not parts or not name or not os.path.isdir(full):
        return jsonify(error="bad request"), 400
    os.makedirs(os.path.join(full, name), exist_ok=True)
    return jsonify(ok=True, name=name)

@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    try: full, parts = resolve(request.form.get("path", ""), create=True)
    except ValueError: return jsonify(error="bad path"), 400
    if not f or not f.filename or not parts:
        return jsonify(error="bad request"), 400
    name = unique(full, secure_filename(f.filename) or "file")
    f.save(os.path.join(full, name))
    return jsonify(ok=True, name=name)

@app.get("/files/<path:p>")
def download(p):
    try: parts = clean(p)
    except ValueError: return "Not found", 404
    return send_from_directory(UPLOAD_DIR, "/".join(parts), as_attachment=True)

@app.delete("/api/files")
def delete_file():
    try: full, parts = resolve(request.args.get("path", ""))
    except ValueError: return jsonify(error="bad path"), 400
    if not parts: return jsonify(error="bad request"), 400
    if os.path.isdir(full):
        if os.listdir(full): return jsonify(error="Folder is not empty"), 409
        os.rmdir(full)
    elif os.path.isfile(full):
        os.remove(full)
    return jsonify(ok=True)

ICON = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAgAAAAIAAgMAAACJFjxpAAAACVBMVEXp7vBfs88dOkrEHaOYAAAEDElEQVR42u3dMW4aQRTGcTMCCVFR5ABbcgyOQJHCZY4yR3EZRS58hD0G5R4ghasIyWhJFxubfe97j5Vgoz9dZr+Bn+fNsGspeZn9erjtqzwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAG78mjvX+8cLg6uni9nT84XBxe6qFfjzqI++PeujKqD/oY+fXi5nh8YlwH5wZb4O/R7KvuUBfR289GUJTu1g9iUN2BubQ14AZwlMQA1ca41smwT0Fq77VAEr+5oE7M0DKlfArkFJVuDz1dbMtilAb3+DdHIFzBoYgIP9pmeXj3b2mAJ0zl1C3Wb2ApXsFji/3jrZdorPA703tZP3oFWikt2DZ4Gjlz0mAJ33pr28B60luuM9UN25VT4ERuJ+V6D353byITB2yf2uwMGfe5BPoRHhN6OS/h76sE9f/eyJEgAIA6owucq3guEMJQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABjlr3RuheyWEgCIAprA3LWfnVGCMGDpz/0XmfvZ+fRWQJA17g57f60nuAn9m0F1v+j9W8E9r0ATmOp+E80SgKV8CoVzOE8AinwIhGOwnuTdsMqHwD8G2wygCdDX2T1oAZbyHnR34TwFKPIedHfhOvcpVd4C3ibY5gAb6z1X53/8ZmUXyQeSIlfAqcE6+yE1cG2bq4AN2MgVMGuwSD8TluElePpy1Id/zF3+oXQjL4CxBIsrnorLkz4+G/hBZ7trHstXP/XRxXd99IOP/3AZAAAAAAAAAAAAAAAAAAAAuDVgzH7Fkay6ApF+xZFRFRDpVxzJyoBIv+JIVgVE+hVHsjIg0q84kpUBNXCtJt9nrH7FkawMiPQrjmRlQH3Q6xPJqoBIv+JIVgZE+hVHsjKgc+4S2awMqM59qiazk3keiPQrjmRlQKRfcSQrAzrvTftcdjp7oLpzayo7mRWI9CuOZKezApF+xZEsvxnJgM6f22eylACADKjC5JrIUgIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAZUIXJNZGlBABkQBOY21zxOZRglH7Fkex0ViDSrziSndAm9L/gayo7nRVoAlOb/MeM0694mT6FI/UrLulD8N/0K67ZQzBWv+Im/WOO1K94md2DY/UrLtk9eNf//D/UrziSlQGRfsUlWYH7boIR6le8yVVgvH7Fkax+fiL9ijepBRixX3Ekq3+DRPoVR0bfX/QrBgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAG4O+Auc5coABPGx2QAAAABJRU5ErkJggg==")

@app.get("/icon.png")
def icon():
    return Response(ICON, mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})

@app.get("/manifest.json")
def manifest():
    return jsonify(name=APP_NAME, short_name=APP_NAME, start_url="/", display="standalone",
                   background_color="#1d3a4a", theme_color="#1d3a4a",
                   icons=[{"src": "/icon.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}])

@app.get("/")
def index():
    html = PAGE.replace("__LAT__", str(LAT)).replace("__LON__", str(LON)) \
               .replace("__UNITS__", UNITS).replace("__WEEKSTART__", str(WEEK_START)).replace("__NAME__", APP_NAME).replace("__LINKS__", json.dumps(LINKS))
    return Response(html, mimetype="text/html")

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1d3a4a">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.png">
<link rel="apple-touch-icon" href="/icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="__NAME__">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>__NAME__</title>
<style>
:root{--bg:#e9eef0;--panel:#f7f9fa;--ink:#18262e;--mute:#5d707b;--line:#cdd8dd;--accent:#1f6f8b;--hot:#c4532d;--ok:#3d8361}
@media(prefers-color-scheme:dark){:root{--bg:#10191f;--panel:#182630;--ink:#e6eef1;--mute:#8aa0ac;--line:#2a3d49;--accent:#5fb3cf;--hot:#e5835d;--ok:#6cc096}}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:16px/1.5 "Avenir Next",Avenir,"Segoe UI",system-ui,sans-serif;
height:100vh;height:100dvh;display:flex;flex-direction:column;overflow:hidden;padding-top:env(safe-area-inset-top)}
nav{display:flex;justify-content:center;gap:6px;padding:6px 12px 0}
nav button{background:none;color:var(--mute);border:0;border-bottom:3px solid transparent;border-radius:0;padding:6px 16px;font-weight:600}
nav button.on{color:var(--accent);border-bottom-color:var(--accent)}
.pages{flex:1;min-height:0;display:flex;overflow-x:auto;overflow-y:hidden;scroll-snap-type:x mandatory;scrollbar-width:none}
.pages::-webkit-scrollbar{display:none}
.page{flex:0 0 100%;min-width:0;overflow-y:auto;scroll-snap-align:start;scroll-snap-stop:always;padding:12px 12px max(20px,env(safe-area-inset-bottom))}
main{max-width:960px;margin:0 auto;display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
@media(min-width:760px){main{grid-template-columns:repeat(4,1fr)}}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px;min-width:0;display:flex;flex-direction:column}
.w2{grid-column:span 2}
.w4{grid-column:1/-1}
.drop{border:2px dashed var(--line);border-radius:12px;padding:22px;text-align:center;color:var(--mute)}
.drop.over{border-color:var(--accent);background:var(--bg)}
.drop p{margin:0 0 10px}
.frow{display:flex;align-items:center;gap:10px;padding:10px 0;border-top:1px solid var(--line)}
.frow:first-child{border-top:0}
.fname{flex:1;min-width:0;word-break:break-all}
.fmeta{color:var(--mute);font-size:.85rem}
.frow a{color:var(--accent);text-decoration:none;border:1px solid var(--line);border-radius:10px;padding:6px 12px}
.xbtn{background:none;color:var(--mute);padding:4px 10px;font-size:1.2rem}
.prog{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin:6px 0 2px}
.prog i{display:block;height:100%;width:0;background:var(--accent)}
.drop button+button{margin-left:8px}
.alt{background:var(--bg);color:var(--ink);border:1px solid var(--line)}
.lnk{background:none;color:var(--accent);padding:2px 8px}
.crumb{display:flex;flex-wrap:wrap;align-items:center;margin-bottom:8px;color:var(--mute)}
.crumb button{background:none;color:var(--accent);padding:2px 6px;border-radius:6px}
.dirbtn{background:none;color:var(--ink);text-align:left;flex:1;min-width:0;padding:0;font-weight:600;word-break:break-all}
.tag{color:var(--accent);font-size:.8rem;font-weight:400;margin-left:6px}
.whead{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.whead h2{margin:0;flex:1}
.whead button{background:var(--bg);color:var(--ink);border:1px solid var(--line);padding:4px 12px}
.week{display:grid;grid-template-columns:1fr;gap:8px}
@media(min-width:900px){.week{grid-template-columns:repeat(7,1fr)}}
.day{background:var(--bg);border:1px solid transparent;border-radius:10px;padding:10px;min-width:0}
.day.today{border-color:var(--accent)}
.dh{font-weight:600;margin-bottom:4px}
.day.today .dh{color:var(--accent)}
.day ul{max-height:none}
.day li{padding:6px 0;gap:8px}
.day li input{width:18px;height:18px}
.day li button{padding:0 6px}
.day form{margin:6px 0 0}
.day form input{font-size:.9rem;padding:6px 8px;width:100%;background:var(--panel)}
.carhead{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.carhead h2{margin:0;flex:1}
.carhead button{background:var(--bg);color:var(--ink);border:1px solid var(--line);padding:4px 12px;font-size:1.1rem}
.car{display:flex;overflow-x:auto;scroll-snap-type:x mandatory;scrollbar-width:none;gap:12px}
.car::-webkit-scrollbar{display:none}
.slide{flex:0 0 100%;scroll-snap-align:start;min-width:0}
.sname{font-size:1.15rem;font-weight:600;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--ok)}
.dot.off{background:var(--hot)}
.sgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
@media(min-width:760px){.sgrid{grid-template-columns:repeat(4,1fr)}}
.cell{background:var(--bg);border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:6px}
.cell small{color:var(--mute)}
.cell b{font-size:1.4rem;font-variant-numeric:tabular-nums}
.cell .bar{margin-top:0}
.sup{color:var(--mute);margin-top:10px;font-size:.9rem}
.dots{display:flex;justify-content:center;gap:8px;margin-top:10px}
.dots button{width:10px;height:10px;padding:0;border-radius:50%;background:var(--line)}
.dots button.on{background:var(--accent)}
h2{margin:0 0 8px;font-size:.9rem;font-weight:600;color:var(--mute)}
.big{font-size:1.9rem;font-weight:600;letter-spacing:-.02em;line-height:1.1;font-variant-numeric:tabular-nums}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:auto}
.bar i{display:block;height:100%;width:0;background:var(--ok);transition:width .6s}
.bar i.warn{background:var(--hot)}
.stat .big{margin-bottom:12px}
#clock{font-size:clamp(2.8rem,10vw,4rem);font-weight:300;letter-spacing:-.04em;line-height:1}
#date{color:var(--mute);margin-top:6px}
#wx{font-size:1.05rem}
form{display:flex;gap:8px;margin-bottom:8px}
input[type=text],textarea{flex:1;font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:8px 12px;min-width:0}
textarea{width:100%;flex:1;min-height:140px;resize:vertical}
button{font:inherit;border:0;border-radius:10px;padding:8px 14px;background:var(--accent);color:#fff;cursor:pointer}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
ul{list-style:none;margin:0;padding:0;max-height:260px;overflow-y:auto}
li{display:flex;align-items:center;gap:12px;padding:8px 0;border-top:1px solid var(--line)}
li:first-child{border-top:0}
li input{width:22px;height:22px;accent-color:var(--accent)}
li span{flex:1;word-break:break-word}
li.done span{color:var(--mute);text-decoration:line-through}
li button{background:none;color:var(--mute);padding:4px 10px;font-size:1.2rem}
.empty{color:var(--mute)}
.links{display:flex;flex-wrap:wrap;gap:8px}
.links a{color:var(--accent);text-decoration:none;border:1px solid var(--line);border-radius:10px;padding:8px 14px}
#saved{color:var(--mute);font-size:.85rem;margin-top:6px;min-height:1.2em}
</style></head><body><nav id="tabs" aria-label="Pages"><button type="button" class="on">Home</button><button type="button">Calendar</button><button type="button">Files</button></nav>
<div class="pages" id="pages">
<section class="page" aria-label="Home"><main>
<div class="tile w2"><div id="clock">--:--</div><div id="date"></div></div>
<div class="tile w2"><h2>Weather</h2><div id="wx"></div></div>


<div class="tile stat"><h2>CPU</h2><div class="big" id="v-cpu">--</div><div class="bar"><i id="b-cpu"></i></div></div>
<div class="tile stat"><h2>Memory</h2><div class="big" id="v-mem">--</div><div class="bar"><i id="b-mem"></i></div></div>
<div class="tile stat"><h2>Disk</h2><div class="big" id="v-disk">--</div><div class="bar"><i id="b-disk"></i></div></div>
<div class="tile stat"><h2>Temperature</h2><div class="big" id="v-temp">--</div><div class="bar"><i id="b-temp"></i></div></div>

<div class="tile w4" id="peertile"><div class="carhead"><h2>Other Pis</h2>
<button type="button" id="pprev" aria-label="Previous Pi">‹</button><button type="button" id="pnext" aria-label="Next Pi">›</button></div>
<div class="car" id="car" tabindex="0" aria-label="Other Pis"></div><div class="dots" id="pdots"></div></div>

<div class="tile w2"><h2>Pi uptime</h2><div class="big" id="up">--</div></div>
<div class="tile w2"><h2>Links</h2><div class="links" id="links"></div></div>

<div class="tile w2"><h2>To do</h2>
<form id="tf"><input type="text" id="ti" placeholder="Add a task" aria-label="New task" autocomplete="off"><button>Add</button></form>
<ul id="tl"></ul></div>

<div class="tile w2"><h2>Notes</h2><textarea id="notes" aria-label="Notes" placeholder="Write anything here. It saves automatically."></textarea><div id="saved"></div></div>
</main></section>
<section class="page" aria-label="Calendar"><main>
<div class="tile w4"><div class="whead"><h2 id="wrange">This week</h2>
<button type="button" id="wprev" aria-label="Previous week">‹</button><button type="button" id="wtoday">Today</button><button type="button" id="wnext" aria-label="Next week">›</button></div>
<div class="week" id="week"></div></div>
</main></section>
<section class="page" aria-label="Files"><main>
<div class="tile w4"><h2>Send files to the Pi</h2>
<div class="fmeta">This device: <b id="devname">…</b><button type="button" id="devren" class="lnk">Rename</button></div>
<div class="fmeta" id="target" style="margin-bottom:10px"></div>
<div class="drop" id="drop"><p>Drop files here, or</p><button type="button" id="pick">Choose files</button><button type="button" id="newf" class="alt">New folder</button><input type="file" id="fin" multiple hidden></div>
<div id="ups"></div></div>
<div class="tile w4"><div class="whead"><h2>Browse</h2><span class="fmeta" id="free"></span></div><div class="crumb" id="crumb"></div><div id="fl"></div></div>
</main></section>
</div>
<script>
const $=id=>document.getElementById(id);
const LAT=__LAT__,LON=__LON__,UNITS="__UNITS__",LINKS=__LINKS__;
let todos=[];

// clock
function tick(){const n=new Date();
$("clock").textContent=n.toLocaleTimeString([],{hour:"numeric",minute:"2-digit"});
$("date").textContent=n.toLocaleDateString([],{weekday:"long",month:"long",day:"numeric"});}
tick();setInterval(tick,10000);

// weather (Open-Meteo)
const WX={0:"Clear",1:"Mostly clear",2:"Partly cloudy",3:"Overcast",45:"Fog",48:"Fog",51:"Light drizzle",53:"Drizzle",55:"Heavy drizzle",61:"Light rain",63:"Rain",65:"Heavy rain",71:"Light snow",73:"Snow",75:"Heavy snow",80:"Showers",81:"Showers",82:"Heavy showers",95:"Thunderstorm",96:"Thunderstorm",99:"Thunderstorm"};
async function weather(){try{
const u=`https://api.open-meteo.com/v1/forecast?latitude=${LAT}&longitude=${LON}&current=temperature_2m,weather_code&daily=temperature_2m_max,temperature_2m_min&temperature_unit=${UNITS}&timezone=auto&forecast_days=1`;
const j=await (await fetch(u)).json(),d=UNITS=="celsius"?"°C":"°F";
$("wx").textContent=`${Math.round(j.current.temperature_2m)}${d}, ${WX[j.current.weather_code]||"—"}. High ${Math.round(j.daily.temperature_2m_max[0])}, low ${Math.round(j.daily.temperature_2m_min[0])}.`;
}catch(e){$("wx").textContent="Weather unavailable. Check the Pi's internet connection."}}
weather();setInterval(weather,900000);

// pi stats
function setBar(k,pct,label,warnAt){$("b-"+k).style.width=Math.min(100,pct)+"%";
$("b-"+k).className=pct>=warnAt?"warn":"";$("v-"+k).textContent=label}
async function stats(){try{const s=await (await fetch("/api/stats")).json();
setBar("cpu",s.cpu,Math.round(s.cpu)+"%",85);setBar("mem",s.mem,Math.round(s.mem)+"%",85);
setBar("disk",s.disk,Math.round(s.disk)+"%",90);
if(s.temp!=null)setBar("temp",s.temp,Math.round(s.temp)+"°C",70);else $("v-temp").textContent="n/a";
const d=Math.floor(s.uptime/86400),h=Math.floor(s.uptime%86400/3600),m=Math.floor(s.uptime%3600/60);
$("up").textContent=(d?d+"d ":"")+h+"h "+m+"m";}catch(e){$("up").textContent="Offline"}}
stats();setInterval(stats,5000);

// swipe pages
const pages=$("pages"),tabs=document.querySelectorAll("#tabs button");
const pidx=()=>Math.round(pages.scrollLeft/(pages.clientWidth||1));
function tabsOn(){const i=pidx();tabs.forEach((b,k)=>{b.className=k==i?"on":"";b.setAttribute("aria-current",k==i?"page":"false")})}
tabs.forEach((b,k)=>b.onclick=()=>pages.scrollTo({left:k*pages.clientWidth,behavior:"smooth"}));
pages.addEventListener("scroll",tabsOn);tabsOn();

// files
const fsz=b=>b<1024?b+" B":b<1048576?(b/1024).toFixed(0)+" KB":b<1073741824?(b/1048576).toFixed(1)+" MB":(b/1073741824).toFixed(2)+" GB";
const ls={get:k=>{try{return localStorage.getItem(k)}catch(e){return null}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch(e){}}};
const guess=()=>{const u=navigator.userAgent;return/iPhone/.test(u)?"iPhone":/iPad/.test(u)?"iPad":/Android/.test(u)?"Android phone":/Windows/.test(u)?"Windows PC":/Mac/.test(u)?"Mac":/Linux/.test(u)?"Linux PC":"My device"};
let cwd="",dev={name:"",slug:""};
const jpost=(u,b)=>fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});
const join=n=>cwd?cwd+"/"+n:n;
const target=()=>cwd||dev.slug;
async function regDevice(name,old){const j=await (await jpost("/api/device",{name,old})).json();
dev={name,slug:j.slug};ls.set("dev_name",name);ls.set("dev_slug",j.slug);$("devname").textContent=name;return j.slug}
async function initDevice(){try{await regDevice(ls.get("dev_name")||guess(),ls.get("dev_slug")||"");cwd=dev.slug;loadFiles()}
catch(e){$("fl").innerHTML='<div class="empty">Cannot reach the Pi.</div>'}}
$("devren").onclick=async()=>{const n=prompt("Name this device",dev.name);if(!n||!n.trim())return;
const old=dev.slug,slug=await regDevice(n.trim(),old);
if(cwd===old)cwd=slug;else if(cwd.startsWith(old+"/"))cwd=slug+cwd.slice(old.length);loadFiles()};
async function del(path,label){if(!confirm("Delete "+label+"?"))return;
const r=await fetch("/api/files?path="+encodeURIComponent(path),{method:"DELETE"});
if(!r.ok){const j=await r.json().catch(()=>({}));alert(j.error||"Could not delete")}loadFiles()}
async function loadFiles(){try{
const r=await fetch("/api/files?path="+encodeURIComponent(cwd));
if(!r.ok){if(cwd){cwd=dev.slug;return loadFiles()}throw 0}
const j=await r.json();
$("free").textContent=fsz(j.free)+" free";$("target").textContent="Uploading to: "+target();
const crumb=$("crumb");crumb.innerHTML="";
const mk=(label,path)=>{const b=document.createElement("button");b.type="button";b.textContent=label;b.onclick=()=>{cwd=path;loadFiles()};return b};
crumb.append(mk("Devices",""));let acc="";
cwd.split("/").filter(Boolean).forEach(seg=>{acc=acc?acc+"/"+seg:seg;crumb.append(document.createTextNode(" › "),mk(seg,acc))});
const box=$("fl");box.innerHTML="";
if(!j.folders.length&&!j.files.length){box.innerHTML='<div class="empty">'+(cwd?"This folder is empty.":"No devices yet.")+"</div>";return}
j.folders.forEach(f=>{const r=document.createElement("div");r.className="frow";
const b=document.createElement("button");b.type="button";b.className="dirbtn";b.textContent="📁 "+f.name;
if(!cwd&&f.name==dev.slug){const t=document.createElement("span");t.className="tag";t.textContent="this device";b.append(t)}
b.onclick=()=>{cwd=join(f.name);loadFiles()};
const m=document.createElement("span");m.className="fmeta";m.textContent=f.count+(f.count==1?" item":" items");
const x=document.createElement("button");x.type="button";x.className="xbtn";x.textContent="×";x.setAttribute("aria-label","Delete folder "+f.name);
x.onclick=()=>del(join(f.name),"folder "+f.name);
r.append(b,m,x);box.append(r)});
j.files.forEach(f=>{const r=document.createElement("div");r.className="frow";
const n=document.createElement("div");n.className="fname";
const nm=document.createElement("div");nm.textContent=f.name;
const mt=document.createElement("div");mt.className="fmeta";
mt.textContent=fsz(f.size)+" · "+new Date(f.mtime*1000).toLocaleString([],{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"});
n.append(nm,mt);
const a=document.createElement("a");a.href="/files/"+join(f.name).split("/").map(encodeURIComponent).join("/");a.textContent="Download";a.setAttribute("download",f.name);
const x=document.createElement("button");x.type="button";x.className="xbtn";x.textContent="×";x.setAttribute("aria-label","Delete "+f.name);
x.onclick=()=>del(join(f.name),f.name);
r.append(n,a,x);box.append(r)})}catch(e){$("fl").innerHTML='<div class="empty">Cannot load files.</div>'}}
$("newf").onclick=async()=>{if(!target())return;const n=prompt("New folder name");if(!n||!n.trim())return;
const base=target(),r=await jpost("/api/mkdir",{path:base,name:n.trim()});
if(!r.ok){alert("Could not create the folder");return}cwd=base;loadFiles()};
const queue=[];let busy=false;
function addFiles(list){if(!target())return;const p=target();[...list].forEach(f=>queue.push({f,p}));nextUp()}
function nextUp(){if(busy||!queue.length)return;busy=true;const {f,p}=queue.shift();
const row=document.createElement("div");row.className="frow";
const info=document.createElement("div");info.className="fname";
const t=document.createElement("div");t.textContent=f.name+"  →  "+p;
const pr=document.createElement("div");pr.className="prog";const bar=document.createElement("i");pr.append(bar);
const st=document.createElement("div");st.className="fmeta";st.textContent="Uploading…";
info.append(t,pr,st);row.append(info);$("ups").prepend(row);
const x=new XMLHttpRequest();x.open("POST","/api/upload");
x.upload.onprogress=e=>{if(e.lengthComputable){bar.style.width=(e.loaded/e.total*100)+"%";st.textContent=fsz(e.loaded)+" of "+fsz(e.total)}};
const fail=m=>{busy=false;st.textContent=m;setTimeout(()=>row.remove(),8000);nextUp()};
x.onload=()=>{if(x.status==200){busy=false;row.remove();loadFiles();nextUp()}else fail(x.status==413?"Too large":"Failed ("+x.status+")")};
x.onerror=()=>fail("Failed. Check the connection.");
const fd=new FormData();fd.append("file",f);fd.append("path",p);x.send(fd)}
const dz=$("drop");
["dragenter","dragover"].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.add("over")}));
["dragleave","drop"].forEach(ev=>dz.addEventListener(ev,e=>{e.preventDefault();dz.classList.remove("over")}));
dz.addEventListener("drop",e=>addFiles(e.dataTransfer.files));
["dragover","drop"].forEach(ev=>window.addEventListener(ev,e=>e.preventDefault()));
$("pick").onclick=()=>$("fin").click();
$("fin").onchange=e=>{addFiles(e.target.files);e.target.value=""};
initDevice();

// week calendar
const WEEKSTART=__WEEKSTART__;
let events={},wk=0;
const dkey=d=>d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
function weekStart(off){const n=new Date();n.setHours(12,0,0,0);n.setDate(n.getDate()-(n.getDay()-WEEKSTART+7)%7+off*7);return n}
function saveEv(){for(const k in events)if(!events[k].length)delete events[k];post({events})}
function renderWeek(focusKey){
const s=weekStart(wk),box=$("week"),e=new Date(s);e.setDate(e.getDate()+6);box.innerHTML="";
const f=d=>d.toLocaleDateString([],{month:"short",day:"numeric"});
$("wrange").textContent=f(s)+" – "+f(e);
const today=dkey(new Date());
for(let i=0;i<7;i++){const d=new Date(s);d.setDate(s.getDate()+i);const k=dkey(d);
const label=d.toLocaleDateString([],{weekday:"short"})+" "+d.getDate();
const day=document.createElement("div");day.className="day"+(k==today?" today":"");
const h=document.createElement("div");h.className="dh";h.textContent=label;
const ul=document.createElement("ul");
(events[k]||[]).forEach((t,j)=>{const li=document.createElement("li");if(t.d)li.className="done";
const cb=document.createElement("input");cb.type="checkbox";cb.checked=t.d;cb.setAttribute("aria-label","Done: "+t.t);
cb.onchange=()=>{t.d=cb.checked;saveEv();renderWeek()};
const sp=document.createElement("span");sp.textContent=t.t;
const x=document.createElement("button");x.type="button";x.textContent="×";x.setAttribute("aria-label","Delete "+t.t);
x.onclick=()=>{events[k].splice(j,1);saveEv();renderWeek()};
li.append(cb,sp,x);ul.append(li)});
const fm=document.createElement("form"),inp=document.createElement("input");
inp.type="text";inp.placeholder="+ Add";inp.autocomplete="off";inp.setAttribute("aria-label","Add task for "+label);
fm.onsubmit=ev=>{ev.preventDefault();const v=inp.value.trim();if(!v)return;
(events[k]=events[k]||[]).push({t:v,d:false});saveEv();renderWeek(k)};
fm.append(inp);day.append(h,ul,fm);box.append(day);
if(focusKey==k)inp.focus()}}
$("wprev").onclick=()=>{wk--;renderWeek()};$("wnext").onclick=()=>{wk++;renderWeek()};$("wtoday").onclick=()=>{wk=0;renderWeek()};
renderWeek();
document.addEventListener("visibilitychange",()=>{if(!document.hidden)fetch("/api/data").then(r=>r.json()).then(d=>{events=d.events||{};renderWeek()})});

// other pis carousel
const car=$("car");
const esc=t=>String(t).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function cell(label,pct,text,warn){const w=pct==null?0:Math.min(100,pct);
return `<div class="cell"><small>${label}</small><b>${text}</b><div class="bar"><i style="width:${w}%" class="${pct>=warn?"warn":""}"></i></div></div>`}
function upTxt(u){const d=Math.floor(u/86400),h=Math.floor(u%86400/3600),m=Math.floor(u%3600/60);return(d?d+"d ":"")+h+"h "+m+"m"}
function slide(p){let h=`<div class="slide"><div class="sname"><span class="dot ${p.online?"":"off"}"></span>${esc(p.name)}</div>`;
if(!p.online)return h+'<div class="empty">Offline. Check the Pi is on and Tailscale is connected.</div></div>';
return h+`<div class="sgrid">${cell("CPU",p.cpu,Math.round(p.cpu)+"%",85)}${cell("Memory",p.mem,Math.round(p.mem)+"%",85)}${cell("Disk",p.disk,Math.round(p.disk)+"%",90)}${p.temp!=null?cell("Temperature",p.temp,Math.round(p.temp)+"°C",70):cell("Temperature",null,"n/a",999)}</div><div class="sup">Up ${upTxt(p.uptime)}</div></div>`}
let pcount=0;
function cur(){return Math.round(car.scrollLeft/(car.clientWidth||1))}
function goto(i){car.scrollTo({left:i*car.clientWidth,behavior:"smooth"})}
function dots(){const i=cur();document.querySelectorAll("#pdots button").forEach((b,k)=>b.className=k==i?"on":"")}
async function peers(){try{const list=await (await fetch("/api/peers")).json();
if(!list.length){car.innerHTML='<div class="empty">No other Pis yet. Add them to PEERS at the top of app.py.</div>';$("pdots").innerHTML="";pcount=0;return}
const i=cur();car.innerHTML=list.map(slide).join("");
if(list.length!=pcount){pcount=list.length;$("pdots").innerHTML=list.map((p,k)=>`<button type="button" aria-label="Show ${esc(p.name)}"></button>`).join("");
document.querySelectorAll("#pdots button").forEach((b,k)=>b.onclick=()=>goto(k))}
car.scrollLeft=Math.min(i,list.length-1)*car.clientWidth;dots()}catch(e){car.innerHTML='<div class="empty">Can\'t load other Pis.</div>'}}
car.addEventListener("scroll",dots);
$("pprev").onclick=()=>goto(Math.max(0,cur()-1));$("pnext").onclick=()=>goto(Math.min(pcount-1,cur()+1));
peers();setInterval(peers,10000);

// todos
const post=b=>fetch("/api/data",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});
function render(){const ul=$("tl");ul.innerHTML="";
if(!todos.length){ul.innerHTML='<li class="empty">Nothing to do. Add a task above.</li>';return}
todos.forEach((t,i)=>{const li=document.createElement("li");if(t.done)li.className="done";
const cb=document.createElement("input");cb.type="checkbox";cb.checked=t.done;cb.setAttribute("aria-label","Done: "+t.text);
cb.onchange=()=>{t.done=cb.checked;post({todos});render()};
const sp=document.createElement("span");sp.textContent=t.text;
const x=document.createElement("button");x.textContent="×";x.setAttribute("aria-label","Delete "+t.text);
x.onclick=()=>{todos.splice(i,1);post({todos});render()};
li.append(cb,sp,x);ul.append(li)})}
$("tf").onsubmit=e=>{e.preventDefault();const v=$("ti").value.trim();if(!v)return;
todos.unshift({text:v,done:false});$("ti").value="";post({todos});render()};

// notes (autosave)
let nt;$("notes").oninput=()=>{$("saved").textContent="Saving…";clearTimeout(nt);
nt=setTimeout(async()=>{try{await post({notes:$("notes").value});$("saved").textContent="Saved"}catch(e){$("saved").textContent="Not saved. Check connection."}},700)};

// links
LINKS.forEach(([n,u])=>{const a=document.createElement("a");a.href=u;a.textContent=n;$("links").append(a)});

// load saved data
fetch("/api/data").then(r=>r.json()).then(d=>{todos=d.todos||[];events=d.events||{};$("notes").value=d.notes||"";render();renderWeek()});
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
