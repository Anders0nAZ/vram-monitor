#!/usr/bin/env python3
"""VRAM Monitor — local web dashboard for Ollama + ComfyUI GPU usage.

Stdlib only. Polls Ollama's /api/ps (precise per-model VRAM + GPU/CPU split)
and nvidia-smi (total board VRAM + which processes are attached to the GPU),
serves a phone-friendly dashboard with an event log, and includes a built-in
ComfyUI idle watchdog that frees its VRAM after a few minutes of no activity.

Controls live in the dashboard (free ComfyUI now / stop LLMs / toggle auto-unload
/ quit), so the server can run hidden at login with no console window.

Windows/WDDM note: nvidia-smi cannot report per-process VRAM on consumer cards,
so non-Ollama usage (e.g. ComfyUI) is inferred as (board_used - ollama_vram)
and load/unload for it is detected from jumps in that "other" figure.
"""

import json
import os
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen, Request
from urllib.error import URLError

# ---------------------------------------------------------------- config
PORT          = 11435                       # dashboard at http://<host>:11435
HOST          = "0.0.0.0"                   # 0.0.0.0 = reachable from phone (LAN/Tailscale)
POLL_SECONDS  = 2
OLLAMA_URL    = "http://localhost:11434/api/ps"
COMFY_URL     = "http://localhost:8188"
COMFY_HINT    = "comfyui"                    # substring (lowercase) marking ComfyUI's GPU process
IDLE_MINUTES  = 5                            # auto-free ComfyUI after this long idle
AUTO_UNLOAD   = True                         # default state of the idle watchdog
OTHER_DELTA_MB = 400                         # min change in "other" VRAM to log as an event
LOG_FILE      = "vram-monitor.log"
LOG_MAX_BYTES = 2_000_000
EVENTS_KEEP   = 250
SETTLE_CYCLES = 4        # polls to suppress "other" attribution after an ollama load/unload
COMFY_RECENT_CYCLES = 30 # a VRAM drop counts as ComfyUI only if it generated within this many polls

MB = 1024 * 1024
GB = 1024 * 1024 * 1024

# Prevent flashing console windows when shelling out under pythonw.exe (no console).
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ---------------------------------------------------------------- shared state
_lock   = threading.Lock()
_state  = {"ok": False, "msg": "starting…"}
_events = deque(maxlen=EVENTS_KEEP)
_ctl = {
    "auto_unload": AUTO_UNLOAD,
    "idle_minutes": IDLE_MINUTES,
    "comfy_busy": None,
    "comfy_idle_since": None,    # monotonic ts when queue went idle
    "comfy_freed": False,        # already freed this idle period
    "last_free": None,           # human time of last auto/manual free
}


def _now_str():
    return datetime.now().strftime("%H:%M:%S")


def log_event(kind, text, detail=""):
    line = {"time": _now_str(), "kind": kind, "text": text, "detail": detail}
    with _lock:
        _events.appendleft(line)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = f"{stamp}  {kind:<7} {text}" + (f"   {detail}" if detail else "")
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            with open(LOG_FILE, "rb") as f:
                data = f.read()[-LOG_MAX_BYTES // 2:]
            with open(LOG_FILE, "wb") as f:
                f.write(b"... (truncated) ...\n" + data)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(row + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- collectors
def _get_json(url, timeout=2):
    try:
        with urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (URLError, OSError, ValueError):
        return None


def query_ollama():
    data = _get_json(OLLAMA_URL)
    if data is None:
        return None
    out = []
    for m in data.get("models", []):
        size      = m.get("size", 0) or 0
        size_vram = m.get("size_vram", 0) or 0
        gpu_pct   = (size_vram / size * 100) if size else 0.0
        remain    = None
        exp = m.get("expires_at")
        if exp:
            try:
                dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                remain = (dt - datetime.now(timezone.utc)).total_seconds()
            except ValueError:
                remain = None
        out.append({
            "name": m.get("name", "?"),
            "vram_mb": round(size_vram / MB),
            "size_mb": round(size / MB),
            "gpu_pct": round(gpu_pct),
            "keepalive_s": remain,
        })
    return out


def query_nvsmi():
    used = total = util = None
    comfy = False
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
        parts = r.stdout.strip().splitlines()[0].split(",")
        used, total, util = (int(p.strip()) for p in parts[:3])
    except (subprocess.SubprocessError, ValueError, IndexError, OSError):
        pass
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=process_name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
        comfy = COMFY_HINT in r.stdout.lower()
    except (subprocess.SubprocessError, OSError):
        pass
    return used, total, util, comfy


# ---------------------------------------------------------------- comfy controls
def comfy_queue_busy():
    q = _get_json(COMFY_URL + "/queue")
    if q is None:
        return None
    return bool(q.get("queue_running")) or bool(q.get("queue_pending"))


def comfy_free():
    body = json.dumps({"unload_models": True, "free_memory": True}).encode()
    req = Request(COMFY_URL + "/free", data=body,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=15) as r:
            r.read()
        return True
    except (URLError, OSError):
        return False


def stop_all_llms(models):
    stopped = []
    for m in (models or []):
        try:
            subprocess.run(["ollama", "stop", m["name"]], capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
            stopped.append(m["name"])
        except (subprocess.SubprocessError, OSError):
            pass
    return stopped


def comfy_idle_tick(busy):
    """Auto-free ComfyUI VRAM after IDLE_MINUTES with an empty queue."""
    _ctl["comfy_busy"] = busy
    if busy is None:
        return
    if busy:
        if _ctl["comfy_freed"]:
            log_event("INFO", "ComfyUI generating", "model reloading")
        _ctl["comfy_idle_since"] = None
        _ctl["comfy_freed"] = False
        return
    # idle
    if _ctl["comfy_idle_since"] is None:
        _ctl["comfy_idle_since"] = time.monotonic()
    idle_s = time.monotonic() - _ctl["comfy_idle_since"]
    if (_ctl["auto_unload"] and not _ctl["comfy_freed"]
            and idle_s >= _ctl["idle_minutes"] * 60):
        if comfy_free():
            _ctl["comfy_freed"] = True
            _ctl["last_free"] = _now_str()
            log_event("INFO", "ComfyUI auto-unloaded",
                      f"idle {int(idle_s)//60}m")
        else:
            log_event("WARN", "ComfyUI /free failed", "will retry")


# ---------------------------------------------------------------- event detection
class _Prev:
    models   = {}
    spilled  = set()
    other_ref = None
    comfy    = False
    overcommit = False
    first    = True
    prev_ollama_mb = 0
    settle   = 0            # >0 = inside an ollama load/unload transition, suppress "other"
    cycle    = 0
    last_busy_cycle = -9999  # last poll where ComfyUI queue was generating


def detect_events(models, used, total, comfy, comfy_busy):
    p = _Prev
    p.cycle += 1
    cur = {m["name"]: m for m in models} if models is not None else {}
    ollama_event = False
    if models is not None:
        for name, m in cur.items():
            if name not in p.models:
                log_event("LOAD", name, f"{m['vram_mb']/1024:.1f}GB  {m['gpu_pct']}% GPU")
                ollama_event = True
        for name in p.models:
            if name not in cur:
                log_event("UNLOAD", name, "keep-alive expired / stopped")
                ollama_event = True
        now_spilled = {m["name"] for m in models if m["gpu_pct"] < 99}
        for name in now_spilled - p.spilled:
            log_event("WARN", name, f"spilled {100 - cur[name]['gpu_pct']}% to CPU")
        p.spilled = now_spilled
        p.models = cur

    if comfy_busy:
        p.last_busy_cycle = p.cycle

    if used is not None:
        ollama_mb = sum(m["vram_mb"] for m in (models or []))
        other = max(0, used - ollama_mb)

        # A model load/unload desyncs board(nvidia-smi) vs ollama(/api/ps) sampling for a
        # few seconds, so "other" spikes. Suppress + silently re-baseline during transitions
        # so ollama's own ramp isn't misattributed to ComfyUI.
        if ollama_event or abs(ollama_mb - p.prev_ollama_mb) >= 300:
            p.settle = SETTLE_CYCLES
        p.prev_ollama_mb = ollama_mb

        if p.other_ref is None:
            p.other_ref = other
        elif p.settle > 0:
            p.settle -= 1
            p.other_ref = other                      # silent re-baseline, no event
        else:
            delta = other - p.other_ref
            if abs(delta) >= OTHER_DELTA_MB:
                # ComfyUI only *grows* VRAM while actively generating; it *frees* shortly
                # after. Attribute increases only when generating now, decreases only if it
                # generated recently. Anything else is ollama-ramp/desktop noise -> suppress.
                recent = (p.cycle - p.last_busy_cycle) <= COMFY_RECENT_CYCLES
                attributable = comfy_busy if delta > 0 else recent
                if attributable:
                    verb = "LOAD" if delta > 0 else "UNLOAD"
                    sign = "+" if delta > 0 else "-"
                    log_event(verb, "ComfyUI (inferred)",
                              f"{sign}{abs(delta)/1024:.1f}GB  board now {used/1024:.1f}GB")
                p.other_ref = other                  # re-baseline either way (don't re-fire)
    if comfy != p.comfy and not p.first:
        log_event("INFO", "ComfyUI " + ("attached to GPU" if comfy else "released GPU"))
    p.comfy = comfy
    if used is not None and total:
        over = used / total > 0.96
        if over and not p.overcommit:
            log_event("WARN", "board VRAM near full", f"{used/1024:.1f}/{total/1024:.1f}GB")
        p.overcommit = over
    p.first = False


# ---------------------------------------------------------------- poll loop
def poll_loop():
    log_event("INFO", "monitor started", f"port {PORT}")
    while True:
        models = query_ollama()
        used, total, util, comfy = query_nvsmi()
        comfy_busy = comfy_queue_busy()          # poll ComfyUI queue once, share it
        detect_events(models, used, total, comfy, comfy_busy)
        comfy_idle_tick(comfy_busy)

        ollama_mb = sum(m["vram_mb"] for m in (models or []))
        other_mb  = max(0, used - ollama_mb) if used is not None else None
        idle_s = None
        if _ctl["comfy_idle_since"] is not None:
            idle_s = int(time.monotonic() - _ctl["comfy_idle_since"])
        with _lock:
            _state.clear()
            _state.update({
                "ok": True, "ts": _now_str(),
                "ollama_up": models is not None,
                "models": models or [],
                "used_mb": used, "total_mb": total, "util": util,
                "ollama_mb": ollama_mb, "other_mb": other_mb,
                "comfy": comfy,
                "auto_unload": _ctl["auto_unload"],
                "idle_minutes": _ctl["idle_minutes"],
                "comfy_idle_s": idle_s,
                "comfy_freed": _ctl["comfy_freed"],
                "last_free": _ctl["last_free"],
                "events": list(_events)[:60],
            })
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- actions
def do_action(action):
    if action == "free_comfy":
        ok = comfy_free()
        _ctl["comfy_freed"] = ok
        _ctl["last_free"] = _now_str()
        log_event("INFO", "ComfyUI freed (manual)" if ok else "ComfyUI /free failed (manual)")
        return {"ok": ok}
    if action == "stop_llms":
        with _lock:
            models = list(_state.get("models", []))
        names = stop_all_llms(models)
        log_event("INFO", "stopped LLMs (manual)", ", ".join(names) or "none resident")
        return {"ok": True, "stopped": names}
    if action == "toggle_idle":
        _ctl["auto_unload"] = not _ctl["auto_unload"]
        log_event("INFO", f"auto-unload {'ON' if _ctl['auto_unload'] else 'OFF'}")
        return {"ok": True, "auto_unload": _ctl["auto_unload"]}
    if action == "quit":
        log_event("INFO", "monitor quitting (web)")
        threading.Timer(0.4, lambda: os._exit(0)).start()
        return {"ok": True}
    return {"ok": False, "error": "unknown action"}


# ---------------------------------------------------------------- web
PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>VRAM Monitor</title><style>
:root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;
--grn:#3fb950;--amb:#d29922;--red:#f85149;--blu:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:14px}
.wrap{max-width:680px;margin:0 auto}
h1{font-size:15px;margin:0 0 12px;display:flex;justify-content:space-between;align-items:baseline}
h1 .gpu{color:var(--mut);font-weight:400}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px;margin-bottom:12px}
.bar{height:26px;border-radius:6px;background:#21262d;overflow:hidden;position:relative}
.bar>i{display:block;height:100%;width:0;transition:width .4s,background .4s}
.bar>span{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:12px}
.sub{color:var(--mut);font-size:12px;margin:8px 0 0;display:flex;gap:16px;flex-wrap:wrap}
.sub b{color:var(--fg);font-weight:600}
.lbl{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin:0 0 8px}
table{width:100%;border-collapse:collapse}td{padding:4px 0;vertical-align:top}
.dot{color:var(--grn)}.rt{text-align:right;color:var(--mut)}
.ev{display:flex;gap:8px;padding:3px 0;border-top:1px solid #21262d;font-size:12.5px}
.ev:first-child{border-top:0}.ev .t{color:var(--mut);flex:0 0 64px}.ev .k{flex:0 0 64px;font-weight:600}
.k.LOAD{color:var(--grn)}.k.UNLOAD{color:var(--blu)}.k.WARN{color:var(--red)}.k.INFO{color:var(--mut)}
.ev .x{flex:1}.ev .d{color:var(--mut)}
.foot{color:var(--mut);font-size:11px;text-align:center}
.off-pill{color:var(--amb)}
.btns{display:flex;gap:8px;flex-wrap:wrap}
button{font:inherit;font-size:12.5px;color:var(--fg);background:#21262d;border:1px solid var(--bd);
border-radius:7px;padding:7px 11px;cursor:pointer}button:hover{border-color:var(--blu)}
button.on{border-color:var(--grn);color:var(--grn)}button.off{border-color:var(--mut);color:var(--mut)}
button.danger:hover{border-color:var(--red);color:var(--red)}
.idle{color:var(--mut);font-size:12px;margin-top:8px}
.hd{cursor:pointer;user-select:none;color:var(--mut);font-size:11px;text-transform:uppercase;
letter-spacing:.06em;margin:0 0 10px;display:flex;align-items:center;gap:7px}
.hd:hover{color:var(--fg)}
.cv{font-size:9px;transition:transform .15s;display:inline-block}
.card.col .cv{transform:rotate(-90deg)}
.card.col{padding-bottom:12px}
.card.col .bd{display:none}
.bd.events{max-height:240px;overflow-y:auto}
h1 .modebtn{font:inherit;font-size:12px;color:var(--mut);background:transparent;border:1px solid var(--bd);
border-radius:6px;padding:2px 9px;cursor:pointer;line-height:1.3}
h1 .modebtn:hover{color:var(--fg);border-color:var(--blu)}
body.mode-mini .card:not([data-sec=vram]){display:none}
body.mode-bar .card{display:none}
</style></head><body><div class=wrap>
<h1><span>VRAM Monitor <span class=gpu>RTX 3090 · 24 GB</span></span>
  <button class=modebtn id=modebtn onclick=cycleMode() title="cycle: full / monitor-only / bar">▭</button></h1>
<div class=card data-sec=vram>
  <p class=hd onclick="toggleSec('vram')"><span class=cv>▾</span> VRAM</p>
  <div class=bd>
    <div class=bar><i id=bar></i><span id=barlbl></span></div>
    <div class=sub>
      <span>Ollama <b id=s_oll>–</b></span>
      <span>Other/ComfyUI <b id=s_oth>–</b></span>
      <span>GPU util <b id=s_util>–</b></span>
      <span id=s_comfy></span>
    </div>
  </div>
</div>
<div class=card data-sec=ctl>
  <p class=hd onclick="toggleSec('ctl')"><span class=cv>▾</span> Controls</p>
  <div class=bd>
    <div class=btns>
      <button onclick="act('free_comfy')">Free ComfyUI VRAM</button>
      <button onclick="act('stop_llms')">Stop all LLMs</button>
      <button id=b_idle onclick="act('toggle_idle')">Auto-unload …</button>
      <button class=danger onclick="if(confirm('Quit the monitor server?'))act('quit')">Quit monitor</button>
    </div>
    <div class=idle id=idle></div>
  </div>
</div>
<div class=card data-sec=models>
  <p class=hd onclick="toggleSec('models')"><span class=cv>▾</span> Resident models</p>
  <div class=bd><table id=models><tbody></tbody></table></div>
</div>
<div class=card data-sec=events>
  <p class=hd onclick="toggleSec('events')"><span class=cv>▾</span> Events <span style="color:var(--grn)">● live</span></p>
  <div class="bd events" id=events></div>
</div>
<p class=foot id=foot>connecting…</p>
</div><script>
const $=s=>document.querySelector(s);
function gb(mb){return mb==null?'–':(mb/1024).toFixed(1)+'G'}
function mmss(s){if(s==null||s<0)return'';let m=Math.floor(s/60),x=Math.floor(s%60);return m+':'+String(x).padStart(2,'0')}
let _fitted=false;
function fitWindow(){
  try{
    const wrap=document.querySelector('.wrap');
    const contentH=Math.ceil(wrap.getBoundingClientRect().bottom)+14;
    const chromeH=window.outerHeight-window.innerHeight;
    window.resizeTo(window.outerWidth,Math.max(60,Math.min(contentH+chromeH,1100)));
  }catch(e){}
}
function saveCols(){localStorage.setItem('vm_cols',
  JSON.stringify([...document.querySelectorAll('.card.col')].map(c=>c.dataset.sec)));}
function toggleSec(sec){const c=document.querySelector(`[data-sec="${sec}"]`);
  if(c){c.classList.toggle('col');saveCols();fitWindow();}}
let _mode=localStorage.getItem('vm_mode')||'full';
function applyMode(){document.body.className=_mode==='full'?'':'mode-'+_mode;
  $('#modebtn').textContent=_mode;fitWindow();}
function cycleMode(){_mode=_mode==='full'?'mini':(_mode==='mini'?'bar':'full');
  localStorage.setItem('vm_mode',_mode);applyMode();}
function restoreUI(){
  try{(JSON.parse(localStorage.getItem('vm_cols')||'[]')).forEach(s=>{
    const c=document.querySelector(`[data-sec="${s}"]`);if(c)c.classList.add('col');});}catch(e){}
  applyMode();}
async function act(a){
  try{await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:a})});}catch(e){}
  if(a==='quit'){$('#foot').textContent='monitor stopped — you can close this window.';return;}
  tick();
}
async function tick(){
 try{
  const r=await fetch('/api/state');const d=await r.json();
  if(!d.ok){$('#foot').textContent=d.msg||'…';return}
  const used=d.used_mb,total=d.total_mb;const pct=total?Math.round(used/total*100):0;
  const bar=$('#bar');bar.style.width=pct+'%';
  bar.style.background=pct>90?'var(--red)':pct>75?'var(--amb)':'var(--grn)';
  $('#barlbl').textContent=(used!=null?gb(used)+' / '+gb(total)+'  ('+pct+'%)':'nvidia-smi unavailable');
  $('#s_oll').textContent=gb(d.ollama_mb);$('#s_oth').textContent=gb(d.other_mb);
  $('#s_util').textContent=d.util!=null?d.util+'%':'–';
  $('#s_comfy').innerHTML=d.comfy?'<b style=color:var(--blu)>ComfyUI on GPU</b>':'';
  // controls
  const bi=$('#b_idle');bi.textContent='Auto-unload: '+(d.auto_unload?'ON':'OFF');
  bi.className=d.auto_unload?'on':'off';
  let it='';
  if(d.comfy_freed)it='ComfyUI VRAM freed'+(d.last_free?' at '+d.last_free:'');
  else if(d.comfy_idle_s!=null)it='ComfyUI idle '+mmss(d.comfy_idle_s)+
    (d.auto_unload?' — auto-free at '+d.idle_minutes+':00':'');
  else if(d.comfy)it='ComfyUI active';
  $('#idle').textContent=it;
  // models
  const tb=$('#models tbody');tb.innerHTML='';
  if(!d.ollama_up)tb.innerHTML='<tr><td class=off-pill>Ollama unreachable (:11434)</td></tr>';
  else if(!d.models.length)tb.innerHTML='<tr><td style=color:var(--mut)>no models resident</td></tr>';
  else for(const m of d.models){const spill=m.gpu_pct<99;const tr=document.createElement('tr');
    tr.innerHTML=`<td><span class=dot>●</span> ${m.name}</td><td class=rt>${gb(m.vram_mb)}</td>`+
      `<td class=rt style="color:${spill?'var(--red)':'var(--mut)'}">${m.gpu_pct}% GPU</td>`+
      `<td class=rt>${mmss(m.keepalive_s)}</td>`;tb.appendChild(tr);}
  // events
  const ev=$('#events');ev.innerHTML='';
  for(const e of d.events){const row=document.createElement('div');row.className='ev';
    row.innerHTML=`<span class=t>${e.time}</span><span class="k ${e.kind}">${e.kind}</span>`+
      `<span class=x>${e.text}${e.detail?` <span class=d>· ${e.detail}</span>`:''}</span>`;ev.appendChild(row);}
  $('#foot').textContent='updated '+d.ts;
  if(!_fitted){_fitted=true;fitWindow();}
 }catch(err){$('#foot').textContent='disconnected — retrying…';}
}
restoreUI();tick();setInterval(tick,2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            with _lock:
                body = json.dumps(_state).encode()
            self._send(body, "application/json")
        else:
            self._send(PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path.startswith("/api/action"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n).decode() or "{}")
            except (ValueError, OSError):
                payload = {}
            result = do_action(payload.get("action", ""))
            self._send(json.dumps(result).encode(), "application/json")
        else:
            self.send_response(404)
            self.end_headers()


def _lan_ip():
    """Best-effort primary LAN address (no traffic is actually sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _tailscale_ip():
    """Tailscale IPv4 if the CLI is present, else None."""
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=3, creationflags=NO_WINDOW,
        )
        addr = out.stdout.strip().splitlines()
        return addr[0].strip() if out.returncode == 0 and addr else None
    except (OSError, subprocess.SubprocessError):
        return None


def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("VRAM Monitor running:")
    print(f"  local : http://localhost:{PORT}")
    lan = _lan_ip()
    if lan:
        print(f"  LAN   : http://{lan}:{PORT}")
    ts = _tailscale_ip()
    if ts:
        print(f"  phone : http://{ts}:{PORT}  (Tailscale)")
    print("Ctrl+C (or the Quit button) to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
