#!/usr/bin/env python3
"""ComfyUI idle VRAM watchdog.

ComfyUI caches its checkpoint in VRAM and never releases it on its own. This
polls ComfyUI's queue; after IDLE_MINUTES with nothing running/pending, it calls
/free to unload models and free the allocator. ComfyUI lazily reloads the model
on your next generation (a few seconds from RAM/disk), so you pay nothing while
you're away and almost nothing when you come back.

Stdlib only. "Idle" = empty queue; there's no reliable "is Nate at the keyboard"
signal, so an empty queue for N minutes is the proxy.
"""

import json
import time
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError

# ---------------------------------------------------------------- config
COMFY_URL    = "http://localhost:8188"
IDLE_MINUTES = 5          # free VRAM after this long with an empty queue
POLL_SECONDS = 15
LOG_FILE     = "comfy-idle.log"

QUEUE   = COMFY_URL + "/queue"
FREE    = COMFY_URL + "/free"
STATS   = COMFY_URL + "/system_stats"


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def get_json(url, timeout=4):
    try:
        with urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (URLError, OSError, ValueError):
        return None


def queue_busy():
    """True if generating/pending, False if idle, None if ComfyUI unreachable."""
    q = get_json(QUEUE)
    if q is None:
        return None
    return bool(q.get("queue_running")) or bool(q.get("queue_pending"))


def vram_free_mb():
    s = get_json(STATS)
    try:
        return round(s["devices"][0]["vram_free"] / 1024 / 1024)
    except (TypeError, KeyError, IndexError):
        return None


def free_vram():
    body = json.dumps({"unload_models": True, "free_memory": True}).encode()
    req = Request(FREE, data=body, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=15) as r:
            r.read()
        return True
    except (URLError, OSError):
        return False


def main():
    log(f"watchdog started - free after {IDLE_MINUTES} min idle, poll {POLL_SECONDS}s")
    last_active = time.monotonic()
    freed = False
    was_down = False

    while True:
        busy = queue_busy()

        if busy is None:                      # ComfyUI not up
            if not was_down:
                log("ComfyUI unreachable — waiting")
                was_down = True
            time.sleep(POLL_SECONDS)
            continue
        if was_down:
            log("ComfyUI back online")
            was_down = False

        if busy:
            if freed:
                log("generation started — model reloading")
            last_active = time.monotonic()
            freed = False
        else:
            idle_s = time.monotonic() - last_active
            if not freed and idle_s >= IDLE_MINUTES * 60:
                before = vram_free_mb()
                if free_vram():
                    after = vram_free_mb()
                    delta = (after - before) if (before is not None and after is not None) else None
                    detail = f" — freed ~{delta} MB" if delta and delta > 0 else ""
                    log(f"idle {int(idle_s)//60}m → unloaded ComfyUI VRAM{detail} "
                        f"(free now {after} MB)")
                    freed = True
                else:
                    log("idle but /free call failed — will retry")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped")
