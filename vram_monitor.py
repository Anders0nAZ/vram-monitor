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

import http.client
import itertools
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

import schedule                                 # look-ahead calendar (/schedule)

# ---------------------------------------------------------------- config
PORT          = 11435                       # dashboard at http://<host>:11435
HOST          = "0.0.0.0"                   # 0.0.0.0 = reachable from phone (LAN/Tailscale)
POLL_SECONDS  = 2
OLLAMA_PORT   = 11436                       # real Ollama, moved off 11434
OLLAMA_BASE   = f"http://127.0.0.1:{OLLAMA_PORT}"
OLLAMA_URL    = OLLAMA_BASE + "/api/ps"     # poller talks direct, not via the gate
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

# --- admission gate -------------------------------------------------------
GATE_HOST     = "0.0.0.0"                    # clients keep talking to :11434
GATE_PORT     = 11434
RESERVE_MB    = 1024                         # headroom never handed out
MAX_HOLD_SECONDS = 120                       # under the historian 240s timeout
RESERVE_TTL   = 25                           # secs an admitted alloc stays reserved
COST_FILE     = "model-costs.json"
DEFAULT_COST_MB = 4096                       # unknown model
COST_FALLBACK_FACTOR = 1.2                   # disk size -> vram estimate
BIG_MODEL_MB  = 6000                         # banner threshold while ComfyUI runs
GATE_CONNECT_TIMEOUT = 10
GATE_STREAM_TIMEOUT  = 900                   # idle gap, not total duration

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
                record_cost(name, m["vram_mb"])
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

        # republish board totals for the gate, then admit whatever now fits
        _board["used_mb"], _board["total_mb"] = used, total
        gate_tick()
        gate = gate_snapshot()

        # Auto-eviction is deliberately off; surface the case, do not act on it.
        banner = None
        big = [m for m in (models or []) if m["vram_mb"] >= BIG_MODEL_MB]
        if comfy_busy and big:
            banner = (f"ComfyUI is generating while {big[0]['name']} holds "
                      f"{big[0]['vram_mb'] / 1024:.1f}GB of VRAM.")

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
                "gate": gate,
                "banner": banner,
                "auto_unload": _ctl["auto_unload"],
                "idle_minutes": _ctl["idle_minutes"],
                "comfy_idle_s": idle_s,
                "comfy_freed": _ctl["comfy_freed"],
                "last_free": _ctl["last_free"],
                "next_job": schedule.next_job_summary(),
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


# ---------------------------------------------------------------- gate: cost model
# What a request would newly allocate. Learned from what Ollama actually reported
# on past loads (size_vram), which beats guessing from file size on disk.
_costs       = {}                 # normalized model name -> observed VRAM MB
_costs_dirty = False
_tags_cache  = {"at": 0.0, "sizes": {}}

# Board totals, republished by poll_loop. Plain dict so the gate can read them
# without taking _lock (which would invert the _gate_lock -> _lock ordering).
_board = {"used_mb": None, "total_mb": None}


def _norm_model(name):
    if not name:
        return None
    return name if ":" in name else name + ":latest"


def _seed_costs_from_log():
    """Bootstrap the cost table from LOAD lines already sitting in the log."""
    out = {}
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "LOAD" not in line or "(inferred)" in line:
                    continue
                parts = line.split()
                try:
                    i = parts.index("LOAD")
                except ValueError:
                    continue
                if len(parts) < i + 3 or not parts[i + 2].endswith("GB"):
                    continue
                try:
                    mb = int(round(float(parts[i + 2][:-2]) * 1024))
                except ValueError:
                    continue
                key = _norm_model(parts[i + 1])
                if key and mb > 0:
                    out[key] = max(out.get(key, 0), mb)
    except OSError:
        pass
    return out


def load_costs():
    global _costs
    try:
        with open(COST_FILE, "r", encoding="utf-8") as f:
            _costs = {k: int(v) for k, v in json.load(f).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        _costs = {}
    if not _costs:
        _costs = _seed_costs_from_log()
        if _costs:
            save_costs()
    return len(_costs)


def save_costs():
    try:
        with open(COST_FILE, "w", encoding="utf-8") as f:
            json.dump(_costs, f, indent=1, sort_keys=True)
    except OSError:
        pass


def record_cost(name, vram_mb):
    """Called on every observed Ollama load so estimates improve over time."""
    global _costs_dirty
    key = _norm_model(name)
    if not key or not vram_mb:
        return
    # High-water mark, matching _seed_costs_from_log. Under-estimating is the
    # harmful direction - it admits a request that then forces the very spill the
    # gate exists to prevent, while over-estimating only costs a little waiting.
    # A stale high mark self-corrects the moment the model is deleted from
    # model-costs.json, which is also how you reset after shrinking num_ctx.
    mb = int(vram_mb)
    if mb > _costs.get(key, 0):
        _costs[key] = mb
        _costs_dirty = True


def _tag_sizes():
    """Disk sizes from /api/tags, cached 60s - fallback cost for unseen models."""
    now = time.monotonic()
    if _tags_cache["sizes"] and now - _tags_cache["at"] < 60:
        return _tags_cache["sizes"]
    data = _get_json(OLLAMA_BASE + "/api/tags", timeout=4)
    sizes = {}
    if data:
        for m in data.get("models", []):
            key = _norm_model(m.get("name"))
            sz = m.get("size") or 0
            if key and sz:
                sizes[key] = round(sz / MB)
    if sizes:
        _tags_cache.update({"at": now, "sizes": sizes})
    return sizes or _tags_cache["sizes"]


def _resident():
    with _lock:
        return {_norm_model(m["name"]) for m in _state.get("models", [])}


def _resident_rows():
    """Resident models with sizes and keep-alive, for the schedule forecast.
    It needs the decay - a model resident now is gone by the time its keep-alive
    expires, and a 24h look-ahead must not assume it holds forever."""
    with _lock:
        return [dict(m) for m in _state.get("models", [])]


def _board_snapshot():
    with _lock:
        return {"used_mb": _state.get("used_mb"), "total_mb": _state.get("total_mb"),
                "other_mb": _state.get("other_mb")}


def estimate_cost_mb(model):
    """MB this request would newly allocate. 0 when the model is already resident."""
    key = _norm_model(model)
    if key is None:
        return DEFAULT_COST_MB
    if key in _resident():
        return 0                                  # no new allocation - let it through
    if key in _costs:
        return _costs[key]
    sz = _tag_sizes().get(key)
    if sz:
        return int(sz * COST_FALLBACK_FACTOR)
    return DEFAULT_COST_MB


# ---------------------------------------------------------------- gate: queue
_gate_lock = threading.Lock()
_gate_q    = []                   # FIFO of _Waiter
_gate_busy = {}                   # waiter id -> {"model","cost_mb","since"}
_gate_seq  = itertools.count(1)
_gate_stat = {"queued": 0, "admitted": 0, "forced": 0}


class _Waiter:
    __slots__ = ("id", "model", "cost_mb", "event", "since", "forced")

    def __init__(self, wid, model, cost_mb):
        self.id      = wid
        self.model   = model or "?"
        self.cost_mb = cost_mb
        self.event   = threading.Event()
        self.since   = time.monotonic()
        self.forced  = False


def _reserved_locked():
    """Admitted-but-not-yet-visible allocations.

    An admitted request has not shown up in nvidia-smi yet, so without this a
    second request in the same poll window would be admitted against the same
    free VRAM. The reservation expires once the load would be visible.
    """
    now = time.monotonic()
    return sum(v["cost_mb"] for v in _gate_busy.values()
               if now - v["since"] < RESERVE_TTL)


def _fits_locked(cost_mb):
    if cost_mb <= 0:
        return True
    used, total = _board["used_mb"], _board["total_mb"]
    if used is None or not total:
        return True                               # can't measure -> fail open
    free = total - used - _reserved_locked()
    return free - RESERVE_MB >= cost_mb


def _admit_locked(w):
    _gate_busy[w.id] = {"model": w.model, "cost_mb": w.cost_mb,
                        "since": time.monotonic()}


def gate_acquire(model, cost_mb):
    """Block until there is room for cost_mb. Always returns - never fails closed."""
    w = _Waiter(next(_gate_seq), model, cost_mb)
    with _gate_lock:
        if not _gate_q and _fits_locked(cost_mb):
            _admit_locked(w)
            return w
        _gate_q.append(w)
        pos = len(_gate_q)
        _gate_stat["queued"] += 1
    log_event("QUEUE", w.model, f"needs {cost_mb / 1024:.1f}GB - position {pos}")

    got    = w.event.wait(MAX_HOLD_SECONDS)
    waited = time.monotonic() - w.since
    if got:
        with _gate_lock:
            _gate_stat["admitted"] += 1
        log_event("ADMIT", w.model, f"waited {waited:.0f}s")
    else:
        # Fail open. A queue that outlives the callers' own timeouts causes the
        # very failure it exists to prevent.
        with _gate_lock:
            if w in _gate_q:
                _gate_q.remove(w)
            _admit_locked(w)
            w.forced = True
            _gate_stat["forced"] += 1
        log_event("WARN", f"gate forced {w.model}",
                  f"no room after {waited:.0f}s - forwarding anyway")
    return w


def gate_release(w):
    if w is None:
        return
    with _gate_lock:
        _gate_busy.pop(w.id, None)


def gate_tick():
    """Admit whatever now fits. Called from poll_loop; no extra thread."""
    global _costs_dirty
    admitted = []
    with _gate_lock:
        while _gate_q:
            w = _gate_q[0]
            if not _fits_locked(w.cost_mb):
                break        # head-of-line blocking is deliberate: a small request
                             # must not eat headroom a larger queued one waits on
            _gate_q.pop(0)
            _admit_locked(w)
            admitted.append(w)
    for w in admitted:
        w.event.set()
    if _costs_dirty:
        _costs_dirty = False
        save_costs()


def gate_snapshot():
    now = time.monotonic()
    with _gate_lock:
        return {
            "queue": [{"model": w.model, "cost_mb": w.cost_mb,
                       "waited": int(now - w.since)} for w in _gate_q],
            "inflight": len(_gate_busy),
            "stat": dict(_gate_stat),
        }


# ---------------------------------------------------------------- gate: proxy
# Hop-by-hop headers plus the two we always recompute ourselves.
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}

# Metadata and model-management calls: no VRAM allocation, must never queue.
UNGATED_PREFIX = ("/api/ps", "/api/tags", "/api/version", "/api/show", "/api/list",
                  "/api/pull", "/api/push", "/api/copy", "/api/delete",
                  "/api/create", "/api/blobs")

# Calls that can trigger a model load.
GATED_PATHS = ("/api/generate", "/api/chat", "/api/embed", "/api/embeddings",
               "/v1/chat/completions", "/v1/completions", "/v1/embeddings")


def _is_release(bj):
    """keep_alive=0 means unload-now. These FREE VRAM, so queueing them behind a
    wait-for-free-VRAM check would deadlock the only mechanism that produces it
    (image-mode.bat, FreeVram.ps1)."""
    if not isinstance(bj, dict) or "keep_alive" not in bj:
        return False
    return bj.get("keep_alive") in (0, 0.0, "0", "0s", "0m", "0h")


def classify(path, bj):
    p = path.split("?", 1)[0].rstrip("/") or "/"
    if p.startswith(UNGATED_PREFIX):
        return "ungated"
    if p in GATED_PATHS:
        return "release" if _is_release(bj) else "gated"
    return "ungated"                              # unknown path -> fail open


class _GateServer(ThreadingHTTPServer):
    # Python sets SO_REUSEADDR by default. On Windows that lets a second
    # process bind a port another process already owns - both sockets listen
    # and delivery becomes undefined. Refuse, so a port clash is loud.
    allow_reuse_address = False
    daemon_threads = True


class GateHandler(BaseHTTPRequestHandler):
    """Admission-controlling reverse proxy in front of Ollama.

    Every GPU consumer except ComfyUI reaches the card through this, so holding a
    request here is enough to keep ComfyUI from being paged out mid-generation.
    """
    protocol_version = "HTTP/1.1"
    server_version   = "VRAMGate"

    def log_message(self, *a):
        pass

    # ---- helpers
    def _read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            buf = []
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                buf.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(buf)
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    def _fail(self, code, msg):
        body = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- verbs
    def do_GET(self):     self._handle()
    def do_POST(self):    self._handle()
    def do_PUT(self):     self._handle()
    def do_DELETE(self):  self._handle()
    def do_HEAD(self):    self._handle()
    def do_OPTIONS(self): self._handle()

    def _handle(self):
        self._sent = False
        w = None
        try:
            body = self._read_body()
            try:
                bj = json.loads(body.decode("utf-8")) if body else None
            except (ValueError, UnicodeDecodeError):
                bj = None

            if classify(self.path, bj) == "gated":
                model = bj.get("model") if isinstance(bj, dict) else None
                cost  = estimate_cost_mb(model)
                if cost > 0:
                    w = gate_acquire(model, cost)

            self._forward(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass                                   # client walked away mid-stream
        except Exception as exc:                   # the gate must never break traffic
            if not self._sent:
                try:
                    self._fail(502, f"gate: {exc.__class__.__name__}: {exc}")
                except OSError:
                    pass
        finally:
            gate_release(w)

    def _forward(self, body):
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        conn = http.client.HTTPConnection("127.0.0.1", OLLAMA_PORT,
                                          timeout=GATE_CONNECT_TIMEOUT)
        try:
            conn.request(self.command, self.path, body=body or None, headers=hdrs)
            # Connect timeout only. A 27B generation legitimately runs for minutes,
            # so the read deadline has to be far looser than the connect one.
            if conn.sock is not None:
                conn.sock.settimeout(GATE_STREAM_TIMEOUT)
            resp = conn.getresponse()

            clen = resp.getheader("Content-Length")
            self.send_response(resp.status)
            self._sent = True
            for k, v in resp.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)

            if self.command == "HEAD":
                self.send_header("Content-Length", clen if clen is not None else "0")
                self.end_headers()
                return

            # read1() returns as soon as any bytes are available; read() would block
            # until the full buffer fills and turn token streaming into one late dump.
            pump = getattr(resp, "read1", None) or resp.read

            if clen is not None:
                self.send_header("Content-Length", clen)
                self.end_headers()
                left = int(clen)
                while left > 0:
                    chunk = pump(min(65536, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    self.wfile.write(chunk)
                self.wfile.flush()
            else:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = pump(65536)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()             # stream: never accumulate
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        finally:
            conn.close()


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
.k.QUEUE{color:var(--amb)}.k.ADMIT{color:var(--grn)}
.banner{background:#3a2a05;border:1px solid var(--amb);color:var(--amb);border-radius:7px;padding:9px 11px;margin-bottom:10px;font-size:12.5px}
.queue{font-size:12.5px;color:var(--mut)}
.qrow{display:flex;gap:8px;padding:3px 0;border-top:1px solid #21262d}
.qrow:first-child{border-top:0}
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
body.mode-mini .card:not([data-sec=vram]):not([data-sec=gate]){display:none}
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
    <div class=sub><span id=s_next></span></div>
  </div>
</div>
<div class=card data-sec=gate>
  <p class=hd onclick="toggleSec('gate')"><span class=cv>&#9662;</span> Gate <span id=g_pill></span></p>
  <div class=bd>
    <div id=banner class=banner style=display:none></div>
    <div id=queue class=queue></div>
    <div class=sub><span>In flight <b id=g_inf>-</b></span><span>Held <b id=g_q>-</b></span><span>Forced <b id=g_f>-</b></span></div>
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
  const nj=d.next_job;
  $('#s_next').innerHTML=nj
    ? 'next <b>'+nj.task+'</b> in <b>'+(nj.in_s<90?Math.round(nj.in_s)+'s'
        :nj.in_s<5400?Math.round(nj.in_s/60)+'m':(nj.in_s/3600).toFixed(1)+'h')+'</b>'
      +(nj.peak_mb?' · needs <b>'+gb(nj.peak_mb)+'</b>':'')
      +' <a href="/schedule" style="color:var(--blu)">schedule &rarr;</a>'
    : '<a href="/schedule" style="color:var(--blu)">schedule &rarr;</a>';
  // gate
  const g=d.gate||{queue:[],inflight:0,stat:{}};const gs=g.stat||{};
  $('#g_inf').textContent=g.inflight||0;
  $('#g_q').textContent=gs.queued||0;
  $('#g_f').textContent=gs.forced||0;
  const q=$('#queue');
  if(!g.queue.length){q.textContent='nothing waiting';}
  else{q.innerHTML='';for(const w of g.queue){const r=document.createElement('div');
    r.className='qrow';
    r.innerHTML=`<span style=flex:1>${w.model}</span>`+
      `<span class=rt>${gb(w.cost_mb)}</span><span class=rt>${w.waited}s</span>`;
    q.appendChild(r);}}
  $('#g_pill').innerHTML=g.queue.length?
    '<span style=color:var(--amb)>&#9679; '+g.queue.length+' waiting</span>':'';
  const bn=$('#banner');
  if(d.banner){bn.style.display='';
    bn.innerHTML=d.banner+' <button onclick="act(\'stop_llms\')">Evict LLMs</button>';}
  else bn.style.display='none';
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
        elif self.path.startswith("/api/schedule"):
            hours = 6
            q = self.path.partition("?")[2]
            for part in q.split("&"):
                k, _, v = part.partition("=")
                if k == "hours" and v.isdigit():
                    hours = int(v)
            try:
                body = json.dumps(schedule.forecast(hours)).encode()
            except Exception as exc:             # a bad forecast must not 500 the dashboard
                body = json.dumps({"ok": False, "msg": f"forecast failed: {exc!r}"}).encode()
            self._send(body, "application/json")
        elif self.path.startswith("/schedule"):
            self._send(schedule.SCHED_PAGE.encode(), "text/html; charset=utf-8")
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
    known = load_costs()
    threading.Thread(target=poll_loop, daemon=True).start()

    schedule.set_hooks(resident=_resident_rows, board=_board_snapshot, log=log_event)
    schedule.start()

    try:
        gate_srv = _GateServer((GATE_HOST, GATE_PORT), GateHandler)
    except OSError as exc:
        # Dashboard still works; say plainly that nothing is being gated.
        gate_srv = None
        log_event("WARN", "gate NOT started",
                  f":{GATE_PORT} is already in use - is Ollama still on it? ({exc})")
    else:
        threading.Thread(target=gate_srv.serve_forever, daemon=True).start()
        log_event("INFO", "gate started",
                  f":{GATE_PORT} -> :{OLLAMA_PORT}, {known} model costs known")

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("VRAM Monitor running:")
    if gate_srv is not None:
        print(f"  gate  : http://localhost:{GATE_PORT}  ->  ollama :{OLLAMA_PORT}")
    else:
        print(f"  gate  : NOT RUNNING - port {GATE_PORT} already in use")
    print(f"  local : http://localhost:{PORT}")
    print(f"  sched : http://localhost:{PORT}/schedule")
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
