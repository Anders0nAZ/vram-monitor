#!/usr/bin/env python3
"""Look-ahead calendar for scheduled jobs that hit the local LLM stack.

Windows Task Scheduler knows WHEN each job runs and, via each task's own
<Description>, WHAT it does. model-costs.json knows what each model costs in VRAM.
jobs.json joins the two: which models a job loads, and how long it takes.

What this module produces is a forecast over the next 6/12/24 hours: every upcoming
run, its projected VRAM footprint, an estimated duration, and the slots where the
projected peak will not fit - i.e. where the gate is going to start holding requests.

Read-only. Nothing here creates, modifies, enables or runs a scheduled task.

Standalone:  python schedule.py --dump [--hours 24] [--now 2026-09-16T06:00]
"""

import csv
import io
import json
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ---------------------------------------------------------------- config
# Anchor to this script, never the working directory. Python puts the script's
# own directory on sys.path, so `import schedule` works from anywhere - but a
# relative data path does not, and jobs.json is required input. Read from the
# wrong cwd it yields zero tracked jobs and a silently empty calendar.
APP_DIR       = os.path.dirname(os.path.abspath(__file__))
JOBS_FILE     = os.path.join(APP_DIR, "jobs.json")
HIST_FILE     = os.path.join(APP_DIR, "job-history.json")
COST_FILE     = os.path.join(APP_DIR, "model-costs.json")

DEFS_SECONDS   = 300      # bulk task-definition refresh (schtasks /query /xml ONE)
STATUS_SECONDS = 15       # live status poll (schtasks /query /fo CSV)
VERBOSE_EVERY  = 20       # every Nth status poll, use /v for last run + result

SLOT_MINUTES   = 30       # calendar granularity
MAX_OCC        = 400      # per-trigger runaway guard
HIST_KEEP      = 20       # observed durations retained per task
MIN_SAMPLES    = 3        # before a learned median beats the seed
DEFAULT_COST_MB = 4096    # unknown model
FALLBACK_TOTAL_MB = 24576 # if the board is not readable (RTX 3090)
RESERVE_MB     = 1024     # mirrors vram_monitor.RESERVE_MB
OTHER_HOLD_SECONDS = 1800 # how long non-Ollama VRAM is assumed to keep holding
KEEPALIVE_FALLBACK = 300  # resident model with no expiry reported
BASELINE_FLOOR_MB  = 200  # ignore rounding noise in the "other" residual

NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

_DOW = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
        "Friday": 4, "Saturday": 5, "Sunday": 6}

# Triggers with no calendar time of their own. They still show up, in the
# always-on lane, because the VRAM they leave resident is the baseline.
_AMBIENT = {"LogonTrigger", "BootTrigger", "RegistrationTrigger",
            "SessionStateChangeTrigger", "IdleTrigger", "EventTrigger"}

# ---------------------------------------------------------------- shared state
_lock  = threading.Lock()
_defs  = {}        # task name -> definition dict
_status = {}       # task name -> {"status","next","last_run","last_result"}
_hist  = {}        # task name -> {"samples":[s,...], "last_run":iso, "last_dur":s}
_hist_dirty = False
_running = {}      # task name -> monotonic ts when it was first seen Running
_warn  = []        # things the forecast cannot account for
_ready = threading.Event()

# Injected by vram_monitor so this module never imports back into it.
_hooks = {"resident": None, "board": None, "log": None}


def set_hooks(**kw):
    _hooks.update({k: v for k, v in kw.items() if v is not None})


def _log(kind, text, detail=""):
    fn = _hooks.get("log")
    if fn:
        fn(kind, text, detail)


# ---------------------------------------------------------------- small parsers
_DUR_RE = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?$")


def _iso_dur(s):
    """ISO-8601 duration -> seconds. PT15M, PT2H, P1D, PT0S. None if unparseable."""
    if not s:
        return None
    m = _DUR_RE.match(s.strip())
    if not m:
        return None
    y, mo, w, d, h, mi, sec = (float(g) if g else 0 for g in m.groups())
    return int(y * 31536000 + mo * 2592000 + w * 604800 + d * 86400
               + h * 3600 + mi * 60 + sec)


def _parse_dt(s):
    """StartBoundary -> naive LOCAL datetime.

    Boundaries come both ways: '2026-08-30T09:00:00-07:00' carries an offset,
    '2026-09-09T17:05:00' (a self-registered one-shot) does not. Naive means local.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip())
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _f(el, *names):
    cur = el
    for n in names:
        if cur is None:
            return None
        cur = cur.find(NS + n)
    return cur


def _t(el, *names, **kw):
    n = _f(el, *names)
    if n is None or n.text is None:
        return kw.get("default")
    return n.text.strip() or kw.get("default")


def _run(args, timeout=20):
    """schtasks, without flashing a console window under pythonw."""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    out = r.stdout or b""
    # schtasks declares encoding="UTF-16" on the per-task XML form but writes
    # ASCII to a pipe. The bulk form carries no declaration at all, which is
    # exactly why this module only ever uses the bulk form.
    for enc in ("utf-8", "utf-16", "mbcs" if os.name == "nt" else "latin-1"):
        try:
            return out.decode(enc), None
        except (UnicodeDecodeError, LookupError):
            continue
    return out.decode("utf-8", "replace"), None


# ---------------------------------------------------------------- config files
_cache = {"jobs": (0.0, None), "costs": (0.0, None)}


def _load_json(path, key, default):
    """Re-read only when mtime moves, so jobs.json can be edited live."""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return _cache[key][1] if _cache[key][1] is not None else default
    seen, val = _cache[key]
    if val is not None and mt == seen:
        return val
    try:
        with open(path, "r", encoding="utf-8") as f:
            val = json.load(f)
    except (OSError, ValueError):
        val = default
    _cache[key] = (mt, val)
    return val


def _manifest():
    return _load_json(JOBS_FILE, "jobs", {"jobs": [], "daemons": [], "lanes": []})


def _costs():
    raw = _load_json(COST_FILE, "costs", {})
    out = {}
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            pass
    return out


def _norm_model(name):
    if not name:
        return None
    return name if ":" in name else name + ":latest"


def cost_static_mb(model):
    """VRAM this model costs to hold, regardless of whether it happens to be
    resident right now. Deliberately NOT vram_monitor.estimate_cost_mb, which
    answers 0 for a resident model - correct for admitting a request now, wrong
    for forecasting a job that runs in six hours."""
    key = _norm_model(model)
    if not key:
        return DEFAULT_COST_MB
    return _costs().get(key, DEFAULT_COST_MB)


def _profile(name):
    """Manifest entry for a task name, or None."""
    for j in _manifest().get("jobs", []):
        pat = j.get("match", "")
        if j.get("regex"):
            try:
                if re.fullmatch(pat, name):
                    return j
            except re.error:
                continue
        elif pat == name:
            return j
    return None


# ---------------------------------------------------------------- history
def _load_hist():
    global _hist
    try:
        with open(HIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _hist = {k: v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        _hist = {}
    return len(_hist)


def _save_hist():
    global _hist_dirty
    if not _hist_dirty:
        return
    try:
        with open(HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(_hist, f, indent=1, sort_keys=True)
        _hist_dirty = False
    except OSError:
        pass


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _record_duration(name, seconds):
    """Observed wall clock for one run. Median, not high-water: unlike VRAM cost,
    over-estimating a duration is not the safe direction - it would paint
    phantom collisions across the calendar."""
    global _hist_dirty
    if seconds is None or seconds < 0:
        return
    h = _hist.setdefault(name, {"samples": []})
    h["samples"] = (h.get("samples") or [])[-(HIST_KEEP - 1):] + [int(seconds)]
    h["last_dur"] = int(seconds)
    h["last_run"] = datetime.now().isoformat(timespec="seconds")
    _hist_dirty = True


def _duration_for(name, prof):
    """(seconds, source, n, lo, hi) - learned median once there is enough of it."""
    h = _hist.get(name) or {}
    samples = h.get("samples") or []
    seed = int((prof or {}).get("seed_seconds", 60) or 0)
    seed_hi = (prof or {}).get("seed_max_seconds")
    if len(samples) >= MIN_SAMPLES:
        return (int(_median(samples)), "measured", len(samples),
                min(samples), max(samples))
    hi = int(seed_hi) if seed_hi else seed
    return seed, "estimated", len(samples), seed, hi


# ---------------------------------------------------------------- definitions
def refresh_definitions():
    """One bulk schtasks call (~120ms) -> every task definition, indexed by name."""
    txt, err = _run(["schtasks", "/query", "/xml", "ONE"], timeout=30)
    if err or not txt:
        _log("WARN", "schedule: task query failed", err or "no output")
        return 0
    try:
        root = ET.fromstring(txt)
    except ET.ParseError as exc:
        _log("WARN", "schedule: task XML unparseable", str(exc))
        return 0

    manifest = _manifest()
    if not manifest.get("jobs"):
        # Silence here is what made a misconfigured launch look like "nothing
        # scheduled today" for three days. Say which file, and where.
        _log("WARN", "schedule: no jobs configured", f"cannot read {JOBS_FILE}")
        with _lock:
            _warn[:] = [f"jobs.json is empty or unreadable at {JOBS_FILE} - "
                        f"nothing can be forecast until it loads"]
        return 0

    found, unsupported = {}, set()
    for task in root:
        uri = _t(task, "RegistrationInfo", "URI") or ""
        name = uri.lstrip("\\")
        if not name:
            continue
        prof = _profile(name)
        if prof is None:
            continue                     # not a stack job - stays off the calendar
        if _t(task, "Settings", "Enabled") == "false":
            continue

        trigs = _f(task, "Triggers")
        kinds = [c.tag[len(NS):] for c in (list(trigs) if trigs is not None else [])]
        for k in kinds:
            if k not in _AMBIENT and k != "TimeTrigger" and k != "CalendarTrigger":
                unsupported.add(k)

        actions = []
        for ex in (task.iter(NS + "Exec")):
            cmd = (_t(ex, "Command") or "").strip('"')
            arg = _t(ex, "Arguments") or ""
            actions.append((cmd + " " + arg).strip())

        found[name] = {
            "name": name,
            "desc": prof.get("desc") or _t(task, "RegistrationInfo", "Description") or "",
            "note": prof.get("note") or "",
            "lane": prof.get("lane") or ("gpu-light" if prof.get("models") else "cpu"),
            "models": [m for m in (prof.get("models") or [])],
            "prof": prof,
            "triggers": list(trigs) if trigs is not None else [],
            "ambient": all(k in _AMBIENT for k in kinds) if kinds else True,
            "limit_s": _iso_dur(_t(task, "Settings", "ExecutionTimeLimit")),
            "action": actions[0] if actions else "",
            "actions": actions,
        }

    with _lock:
        _defs.clear()
        _defs.update(found)
        _warn[:] = ([f"unsupported trigger type: {k}" for k in sorted(unsupported)])
    return len(found)


# ---------------------------------------------------------------- occurrences
def _expand(trg, t0, t1):
    """Naive-local datetimes this trigger fires at within [t0, t1]."""
    kind = trg.tag[len(NS):]
    if _t(trg, "Enabled") == "false":
        return []
    start = _parse_dt(_t(trg, "StartBoundary"))
    end = _parse_dt(_t(trg, "EndBoundary"))
    horizon = min(t1, end) if end else t1

    bases = []
    if kind == "TimeTrigger":
        if start:
            bases = [start]
    elif kind == "CalendarTrigger":
        if start is None:
            return []
        byday, byweek = _f(trg, "ScheduleByDay"), _f(trg, "ScheduleByWeek")
        bymonth = _f(trg, "ScheduleByMonth")
        if byday is not None:
            n = max(1, int(_t(byday, "DaysInterval", default="1") or 1))
            step = timedelta(days=n)
            cur = start
            if start < t0:                      # jump forward, do not walk months
                cur = start + step * int((t0 - start) / step)
            while cur <= horizon and len(bases) < MAX_OCC:
                if cur >= start:
                    bases.append(cur)
                cur += step
        elif byweek is not None:
            n = max(1, int(_t(byweek, "WeeksInterval", default="1") or 1))
            dow_el = _f(byweek, "DaysOfWeek")
            days = {_DOW[c.tag[len(NS):]] for c in (list(dow_el) if dow_el is not None else [])
                    if c.tag[len(NS):] in _DOW} or {start.weekday()}
            anchor = (start - timedelta(days=start.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0)
            step = timedelta(weeks=n)
            wk = anchor
            if anchor + timedelta(days=7) < t0:
                wk = anchor + step * int((t0 - timedelta(days=7) - anchor) / step)
            while wk <= horizon and len(bases) < MAX_OCC:
                for d in sorted(days):
                    occ = (wk + timedelta(days=d)).replace(
                        hour=start.hour, minute=start.minute, second=start.second)
                    if start <= occ <= horizon:
                        bases.append(occ)
                wk += step
        elif bymonth is not None:
            return []                            # none in use; surfaced as a warning
        else:
            return []
    else:
        return []                                # ambient - handled elsewhere

    rep = _f(trg, "Repetition")
    iv = _iso_dur(_t(rep, "Interval")) if rep is not None else None
    if not iv:
        return sorted({b for b in bases if t0 <= b <= horizon})

    rep_dur = _iso_dur(_t(rep, "Duration")) if rep is not None else None
    step = timedelta(seconds=iv)
    out = set()
    for b in bases:
        limit = min(b + timedelta(seconds=rep_dur), horizon) if rep_dur else horizon
        cur = b
        if b < t0:
            cur = b + step * int((t0 - b) / step)
        n = 0
        while cur <= limit and n < MAX_OCC:
            if cur >= b and cur >= t0:
                out.add(cur)
            cur += step
            n += 1
    return sorted(out)[:MAX_OCC]


# ---------------------------------------------------------------- status poll
def poll_status(verbose=False):
    """Live task status. Also times Ready -> Running -> Ready transitions, which
    is the only start/stop signal available: the TaskScheduler/Operational event
    log is disabled on this box, so there is no events 100/102 history to read."""
    args = ["schtasks", "/query", "/fo", "CSV"] + (["/v"] if verbose else [])
    txt, err = _run(args, timeout=30)
    if err or not txt:
        return 0
    try:
        rows = list(csv.DictReader(io.StringIO(txt)))
    except csv.Error:
        return 0

    with _lock:
        tracked = set(_defs)
        ambient = {n for n, d in _defs.items() if d.get("ambient")}

    seen = {}
    for r in rows:
        name = (r.get("TaskName") or "").lstrip("\\")
        if name not in tracked or name in seen:
            continue                     # CSV emits one row per trigger - dedupe
        seen[name] = {
            "status": (r.get("Status") or "").strip(),
            "next": (r.get("Next Run Time") or "").strip(),
            "last_run": (r.get("Last Run Time") or "").strip() or None,
            "last_result": (r.get("Last Result") or "").strip() or None,
        }

    now = time.monotonic()
    for name, info in seen.items():
        running = info["status"].lower() == "running"
        was = _running.get(name)
        if running and was is None:
            # None means "not running as far as we know". A task already Running
            # at startup gets -1: we never saw it start, so we must not time it.
            _running[name] = now if name in _status else -1.0
        elif not running and was is not None:
            if was > 0 and name not in ambient:
                dur = now - was
                limit = (_defs.get(name) or {}).get("limit_s")
                if dur >= 1 and (not limit or dur <= limit * 1.5):
                    _record_duration(name, dur)
            _running.pop(name, None)

    with _lock:
        for name, info in seen.items():
            cur = _status.setdefault(name, {})
            if not verbose:
                info.pop("last_run", None)
                info.pop("last_result", None)
            cur.update({k: v for k, v in info.items() if v is not None or k in ("status", "next")})
    _save_hist()
    return len(seen)


# ---------------------------------------------------------------- forecast
def _board():
    fn = _hooks.get("board")
    if fn:
        try:
            b = fn() or {}
            if b.get("total_mb"):
                return int(b["total_mb"]), b.get("used_mb"), b.get("other_mb")
        except Exception:
            pass
    return FALLBACK_TOTAL_MB, None, None


def _baseline():
    """VRAM already committed, and how long each piece is expected to hold.

    This term is not decoration - it is usually the deciding one. On 2026-09-09 the
    gate force-admitted a 0.3GB embed because the board was at 23.1/24.0GB: a 16.5GB
    model plus ~6.6GB of NON-Ollama VRAM. Job-vs-job arithmetic alone says that
    morning fit comfortably, so a forecast that only counts scheduled jobs would
    have predicted exactly the wrong thing.

    It decays rather than persisting flat: a model goes when its keep-alive expires,
    and non-Ollama VRAM is assumed to hold for OTHER_HOLD_SECONDS. A forecast six
    hours out must not be shaped by whatever ComfyUI happened to be doing at load.
    """
    items, rows = [], []
    fn = _hooks.get("resident")
    if fn:
        try:
            rows = list(fn() or [])
        except Exception:                        # never let the dashboard die for this
            rows = []
    for m in rows:
        if isinstance(m, str):
            name, mb, ka = m, cost_static_mb(m), None
        else:
            name = m.get("name")
            mb = m.get("vram_mb") or cost_static_mb(name)
            ka = m.get("keepalive_s")
        if not name or not mb:
            continue
        hold = float(ka) if ka is not None else KEEPALIVE_FALLBACK
        items.append({"name": _norm_model(name), "mb": int(mb),
                      "hold_s": max(0.0, hold), "kind": "model"})

    _, _, other = _board()
    if other and other >= BASELINE_FLOOR_MB:
        items.append({"name": "other (ComfyUI / unattributed)", "mb": int(other),
                      "hold_s": float(OTHER_HOLD_SECONDS), "kind": "other"})
    return {"mb": sum(i["mb"] for i in items), "items": items,
            "other_hold_s": OTHER_HOLD_SECONDS}


def forecast(hours=6, now=None):
    """Blocks + 30-minute VRAM rollup for the next `hours`."""
    hours = 6 if hours not in (6, 12, 24) else hours
    now = now or datetime.now()
    origin = now.replace(minute=(now.minute // SLOT_MINUTES) * SLOT_MINUTES,
                         second=0, microsecond=0)
    end = origin + timedelta(hours=hours)

    with _lock:
        defs = dict(_defs)
        status = {k: dict(v) for k, v in _status.items()}
        warns = list(_warn)
    if not defs:
        msg = warns[0] if warns else (
            "no task definitions yet - the first schtasks query has not returned")
        return {"ok": False, "msg": msg, "hours": hours,
                "origin": origin.isoformat(timespec="seconds"), "blocks": [],
                "slots": [], "warnings": warns}

    total_mb, used_mb, other_mb = _board()
    usable_mb = max(1, total_mb - RESERVE_MB)
    base = _baseline()
    now_off = (now - origin).total_seconds()

    blocks = []
    for name, d in sorted(defs.items()):
        prof = d.get("prof") or {}
        dur_s, src, n, lo, hi = _duration_for(name, prof)
        models = [{"name": m, "mb": cost_static_mb(m)} for m in d["models"]]
        st = status.get(name, {})
        running = (st.get("status") or "").lower() == "running"

        if d.get("ambient"):
            blocks.append({
                "id": f"{name}@ambient", "task": name, "lane": d["lane"],
                "ambient": True, "off_s": 0, "dur_s": (end - origin).total_seconds(),
                "start": None, "end": None,
                "dur_s_real": None, "dur_src": src, "dur_n": n,
                "models": models, "peak_mb": sum(m["mb"] for m in models),
                "desc": d["desc"], "note": d["note"], "action": d["action"],
                "running": running, "status": st.get("status"),
                "last_run": st.get("last_run"), "last_result": st.get("last_result"),
                "limit_s": d.get("limit_s"),
            })
            continue

        occs = set()
        for trg in d["triggers"]:
            occs.update(_expand(trg, origin, end))
        for occ in sorted(occs):
            blocks.append({
                "id": f"{name}@{occ.isoformat(timespec='seconds')}",
                "task": name, "lane": d["lane"], "ambient": False,
                "off_s": (occ - origin).total_seconds(),
                "dur_s": dur_s, "dur_lo": lo, "dur_hi": hi,
                "start": occ.isoformat(timespec="seconds"),
                "end": (occ + timedelta(seconds=dur_s)).isoformat(timespec="seconds"),
                "dur_src": src, "dur_n": n,
                "models": models, "peak_mb": sum(m["mb"] for m in models),
                "desc": d["desc"], "note": d["note"], "action": d["action"],
                "running": running, "status": st.get("status"),
                "next": st.get("next"),
                "last_run": st.get("last_run"), "last_result": st.get("last_result"),
                "limit_s": d.get("limit_s"),
            })

    # --- 30-min rollup. A model held by two concurrent jobs is ONE copy in VRAM,
    # so slot cost is the union of model names, not the sum of per-job peaks.
    slots, collisions = [], []
    step = SLOT_MINUTES * 60
    for i in range(int(hours * 3600 // step)):
        s0, s1 = i * step, (i + 1) * step
        # Baseline first: whatever is still holding when this slot starts. Keyed by
        # model name so a job that wants an already-resident model is charged once.
        sizes, held = {}, []
        for it in base["items"]:
            if now_off + it["hold_s"] > s0:
                sizes[it["name"]] = max(sizes.get(it["name"], 0), it["mb"])
                held.append(it["name"])
        # `jobs` names only what actually costs VRAM here, deduped: a 15-minute
        # watchdog lands twice in a 30-minute slot and costs nothing either time,
        # and naming it in a contention warning would just be noise.
        jobs, runs = [], 0
        for b in blocks:
            if b["ambient"]:
                continue
            if b["off_s"] < s1 and (b["off_s"] + max(b["dur_s"], 60)) > s0:
                runs += 1
                if not b["models"]:
                    continue
                for m in b["models"]:
                    sizes[m["name"]] = max(sizes.get(m["name"], 0), m["mb"])
                if b["task"] not in jobs:
                    jobs.append(b["task"])
        mb = sum(sizes.values())
        contended = mb > usable_mb
        slots.append({"off_s": s0,
                      "t": (origin + timedelta(seconds=s0)).isoformat(timespec="minutes"),
                      "mb": mb, "jobs": jobs, "n": len(jobs), "runs": runs,
                      "base_mb": sum(v for k, v in sizes.items() if k in held),
                      "contended": contended})
        if contended:
            collisions.append({"t": slots[-1]["t"], "off_s": s0, "mb": mb,
                               "jobs": jobs or ["baseline alone"],
                               "base_mb": slots[-1]["base_mb"]})

    # Consecutive slots with the same cause are one event, not one warning each -
    # a 35-minute RobonerRefresh would otherwise raise the same banner three times.
    merged = []
    for c in collisions:
        prev = merged[-1] if merged else None
        if prev and prev["jobs"] == c["jobs"] and prev["_next_off"] == c["off_s"]:
            prev["_next_off"] = c["off_s"] + step
            prev["until"] = (origin + timedelta(seconds=c["off_s"] + step)).isoformat(timespec="minutes")
            prev["mb"] = max(prev["mb"], c["mb"])
            prev["base_mb"] = max(prev["base_mb"], c["base_mb"])
        else:
            c = dict(c, until=(origin + timedelta(seconds=c["off_s"] + step)).isoformat(timespec="minutes"),
                     _next_off=c["off_s"] + step)
            merged.append(c)
    for c in merged:
        c.pop("_next_off", None)
    collisions = merged

    upcoming = sorted((b for b in blocks if not b["ambient"] and b["off_s"] >= 0),
                      key=lambda b: b["off_s"])
    nxt = next((b for b in upcoming if b["off_s"] >= now_off), None)

    return {
        "ok": True,
        "ts": now.strftime("%H:%M:%S"),
        "hours": hours,
        "origin": origin.isoformat(timespec="seconds"),
        "now_off_s": now_off,
        "slot_s": step,
        "total_mb": total_mb, "used_mb": used_mb, "other_mb": other_mb,
        "reserve_mb": RESERVE_MB, "usable_mb": usable_mb,
        "baseline": base,
        "daemons": _manifest().get("daemons", []),
        "lanes": _manifest().get("lanes", []),
        "blocks": blocks,
        "slots": slots,
        "collisions": collisions,
        "peak_mb": max((s["mb"] for s in slots), default=0),
        "next": ({"task": nxt["task"], "in_s": nxt["off_s"] - now_off,
                  "peak_mb": nxt["peak_mb"], "start": nxt["start"]} if nxt else None),
        "warnings": warns,
    }


_next = {"at": None, "job": None}


def next_job_summary():
    """One-line teaser for the main dashboard. Read-only against a value the
    schedule thread refreshes - poll_loop runs every 2s and must not pay for a
    full trigger expansion each time."""
    j = _next["job"]
    if not j or not j.get("at_iso"):
        return None
    left = (datetime.fromisoformat(j["at_iso"]) - datetime.now()).total_seconds()
    if left < -300:                              # stale; the thread will replace it
        return None
    return {"task": j["task"], "in_s": max(0, int(left)), "peak_mb": j["peak_mb"]}


def _refresh_next():
    try:
        f = forecast(24)
    except Exception:
        return
    n = f.get("next") if f.get("ok") else None
    _next["job"] = ({"task": n["task"], "peak_mb": n["peak_mb"], "at_iso": n["start"]}
                    if n and n.get("start") else None)
    _next["at"] = time.monotonic()


# ---------------------------------------------------------------- threads
def _loop():
    n = refresh_definitions()
    _load_hist()
    poll_status(verbose=True)
    _refresh_next()
    _ready.set()
    _log("INFO", "schedule started", f"{n} jobs tracked, {len(_hist)} with history")
    last_defs, i = time.monotonic(), 0
    while True:
        time.sleep(STATUS_SECONDS)
        i += 1
        try:
            poll_status(verbose=(i % VERBOSE_EVERY == 0))
            if time.monotonic() - last_defs >= DEFS_SECONDS:
                refresh_definitions()
                last_defs = time.monotonic()
            _refresh_next()
        except Exception as exc:                 # a poll failure must not kill the thread
            _log("WARN", "schedule poll failed", repr(exc))


def start():
    threading.Thread(target=_loop, daemon=True, name="schedule").start()


# ---------------------------------------------------------------- page
SCHED_PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Stack Schedule</title><style>
:root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;
--grn:#3fb950;--amb:#d29922;--red:#f85149;--blu:#58a6ff;--pxh:110px}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:14px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:15px;margin:0 0 12px;display:flex;justify-content:space-between;
align-items:baseline;gap:12px;flex-wrap:wrap}
h1 .gpu{color:var(--mut);font-weight:400}
a{color:var(--blu);text-decoration:none}a:hover{text-decoration:underline}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px;margin-bottom:12px}
.row{display:flex;gap:16px;flex-wrap:wrap;align-items:center}
.btns{display:flex;gap:6px}
button{font:inherit;font-size:12.5px;color:var(--fg);background:#21262d;border:1px solid var(--bd);
border-radius:7px;padding:6px 12px;cursor:pointer}
button:hover{border-color:var(--blu)}
button.on{border-color:var(--blu);color:var(--blu)}
.sum{color:var(--mut);font-size:12.5px;display:flex;gap:18px;flex-wrap:wrap}
.sum b{color:var(--fg);font-weight:600}
.sum b.bad{color:var(--red)}.sum b.warn{color:var(--amb)}
.banner{background:#3a2a05;border:1px solid var(--amb);color:var(--amb);border-radius:7px;
padding:9px 11px;margin-bottom:12px;font-size:12.5px}
.banner.bad{background:#3d1417;border-color:var(--red);color:var(--red)}
.lbl{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin:0 0 8px}

.grid{display:grid;grid-template-columns:52px 40px repeat(4,minmax(0,1fr));gap:0;
border:1px solid var(--bd);border-radius:8px;overflow:hidden;background:#10151c}
.hdr{grid-column:1/-1;display:grid;grid-template-columns:subgrid;border-bottom:1px solid var(--bd)}
.hdr>div{padding:6px 8px;font-size:11px;color:var(--mut);text-transform:uppercase;
letter-spacing:.05em;border-left:1px solid var(--bd);white-space:nowrap;overflow:hidden}
.hdr>div:first-child,.hdr>div:nth-child(2){border-left:0}
.col{position:relative;height:var(--h);border-left:1px solid var(--bd)}
.col.gut,.col.spine{border-left:0}
.col.lanes{background-image:repeating-linear-gradient(to bottom,
  transparent 0,transparent calc(var(--pxh)/2 - 1px),#1b222c calc(var(--pxh)/2 - 1px),#1b222c calc(var(--pxh)/2)),
  repeating-linear-gradient(to bottom,transparent 0,transparent calc(var(--pxh) - 1px),#262d38 calc(var(--pxh) - 1px),#262d38 var(--pxh))}
.hr{position:absolute;left:0;right:4px;text-align:right;color:var(--mut);font-size:11px;
padding-right:5px;transform:translateY(-1px)}
.hr.day{color:var(--blu)}
.now{position:absolute;left:0;right:0;height:0;border-top:1px solid var(--red);z-index:6;pointer-events:none}
.now:after{content:'now';position:absolute;left:2px;top:-13px;font-size:10px;color:var(--red);background:var(--bg);padding:0 3px}

.slot{position:absolute;left:3px;right:3px;border-radius:2px}
.slot.ok{background:rgba(63,185,80,.30)}
.slot.mid{background:rgba(210,153,34,.38)}
.slot.bad{background:rgba(248,81,73,.55)}

.blk{position:absolute;border-radius:5px;padding:2px 5px;overflow:hidden;cursor:pointer;
border:1px solid;font-size:11.5px;line-height:1.25;z-index:2}
.blk:hover{filter:brightness(1.25)}
.blk.sel{outline:2px solid var(--blu);outline-offset:-1px;z-index:5}
.blk .n{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.blk .m{color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.blk.band .n,.blk.band .m{white-space:normal;overflow-wrap:anywhere;line-height:1.2}
.blk.heavy{background:#3d1417;border-color:#7d2b30;color:#ffb3ae}
.blk.light{background:#3a2a05;border-color:#7a5c12;color:#f0c874}
.blk.cpu{background:#12262e;border-color:#245868;color:#8fd3e8}
.blk.amb{background:#1c2128;border-color:#30363d;color:var(--mut)}
.blk.band{background-image:repeating-linear-gradient(45deg,transparent 0 7px,rgba(255,255,255,.045) 7px 14px);
opacity:.85}
.blk.band .m{position:sticky;top:0}
.blk.run{border-color:var(--grn);box-shadow:0 0 0 1px var(--grn) inset}
.chip{display:inline-block;background:rgba(0,0,0,.35);border-radius:3px;padding:0 4px;margin-left:4px}

/* Sticky: a 24h grid is ~1050px tall, so a panel parked under it would never
   be on screen at the moment you click a block. */
.det{min-height:74px;position:sticky;bottom:10px;z-index:20;
box-shadow:0 6px 22px rgba(0,0,0,.65)}
.det .t{font-size:14px;font-weight:600;margin:0 0 6px}
.det .meta{color:var(--mut);display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px;font-size:12px}
.det .meta b{color:var(--fg);font-weight:600}
.det p{margin:0 0 7px}
.det .note{color:var(--mut);border-left:2px solid var(--bd);padding-left:9px}
.det .cmd{color:var(--mut);font-size:11.5px;word-break:break-all}
.hint{color:var(--mut)}

.list{display:none}
.li{border-top:1px solid #21262d;padding:8px 0;cursor:pointer}
.li:first-child{border-top:0}
.li .top{display:flex;gap:10px;align-items:baseline}
.li .tm{color:var(--mut);flex:0 0 52px}
.li .nm{font-weight:600;flex:1}
.li .mb{color:var(--mut)}
.li .d{color:var(--mut);font-size:12px;padding-left:62px}
.li.bad .nm{color:var(--red)}
.foot{color:var(--mut);font-size:11px;text-align:center;margin-top:4px}
@media(max-width:820px){
  .grid{display:none}
  .list{display:block}
  body{padding:10px}
}
</style></head><body><div class=wrap>

<h1><span>Stack Schedule <span class=gpu id=gpu>–</span></span>
  <span class=btns>
    <button data-h=6 onclick="setWin(6)">6h</button>
    <button data-h=12 onclick="setWin(12)">12h</button>
    <button data-h=24 onclick="setWin(24)">24h</button>
    <a href="/" style="align-self:center;margin-left:6px">monitor →</a>
  </span></h1>

<div id=banners></div>

<div class=card>
  <div class=sum id=sum></div>
  <p class=lbl style="margin:11px 0 6px">held now — every job has to fit around this</p>
  <div class=sum id=base></div>
</div>

<div class=card style="padding:0;overflow:hidden">
  <div class=grid id=grid></div>
  <div class=list id=list></div>
</div>

<div class="card det" id=det><p class=hint>Pick a block to see what it does.</p></div>
<p class=foot id=foot>–</p>
</div>
<script>
const $=s=>document.querySelector(s);
const PXH={6:110,12:70,24:44}, DENSE=5;   // >DENSE runs in the window -> cadence band
let WIN=+(localStorage.getItem('sched_win')||6), DATA=null, SEL=null;
if(!PXH[WIN])WIN=6;

const LANES=[['gpu-heavy','GPU heavy','heavy'],['gpu-light','GPU light','light'],
             ['cpu','CPU / net','cpu'],['always-on','Always on','amb']];

function gb(mb){return mb>=1024?(mb/1024).toFixed(1)+'GB':mb+'MB';}
function dur(s){s=Math.round(s);if(s<60)return s+'s';
  if(s<3600)return Math.round(s/60)+'m';
  const h=Math.floor(s/3600),m=Math.round((s%3600)/60);return m?h+'h'+m+'m':h+'h';}
function hhmm(iso){return iso?iso.slice(11,16):'';}
function esc(t){const d=document.createElement('div');d.textContent=t==null?'':t;return d.innerHTML;}

function setWin(h){WIN=h;localStorage.setItem('sched_win',h);tick();}

/* Overlapping runs in one lane get side-by-side sub-columns, like a day view. */
function pack(bs){
  const cols=[];
  bs.sort((a,b)=>a.off_s-b.off_s).forEach(b=>{
    const end=b.off_s+Math.max(b.dur_s,180);
    let i=cols.findIndex(c=>c<=b.off_s);
    if(i<0){i=cols.length;cols.push(end);}else cols[i]=end;
    b._c=i;
  });
  bs.forEach(b=>b._n=cols.length);
  return bs;
}

function render(d){
  const pxh=PXH[d.hours], H=pxh*d.hours;
  document.documentElement.style.setProperty('--pxh',pxh+'px');

  document.querySelectorAll('.btns button').forEach(b=>
    b.classList.toggle('on',+b.dataset.h===d.hours));

  $('#gpu').textContent=(d.used_mb!=null?gb(d.used_mb)+' / ':'')+gb(d.total_mb)
    +' · '+gb(d.usable_mb)+' usable';

  const bad=d.slots.filter(s=>s.contended).length;
  $('#sum').innerHTML=
    '<span>next <b>'+(d.next?esc(d.next.task)+'</b> in <b>'+dur(d.next.in_s):'—</b><b>')+'</b></span>'+
    '<span>baseline <b>'+gb(d.baseline.mb)+'</b></span>'+
    '<span>window peak <b class="'+(d.peak_mb>d.usable_mb?'bad':d.peak_mb>d.usable_mb*.85?'warn':'')+'">'+gb(d.peak_mb)+'</b></span>'+
    '<span>runs <b>'+d.blocks.filter(b=>!b.ambient).length+'</b></span>'+
    '<span>tight slots <b class="'+(bad?'bad':'')+'">'+bad+'</b></span>';

  let ban='';
  (d.collisions||[]).slice(0,3).forEach(c=>{
    const who=c.jobs[0]==='baseline alone'
      ? 'Nothing scheduled is to blame — the '+gb(c.base_mb)+' already resident is itself over the line'
      : esc(c.jobs.join(' + '))+(c.base_mb?', on top of '+gb(c.base_mb)+' already held':'');
    ban+='<div class="banner bad">'+hhmm(c.t)+'–'+hhmm(c.until)+' — '+gb(c.mb)+
      ' projected vs '+gb(d.usable_mb)+' the gate can hand out. '+who+
      '. Expect a queue hold, then a forced admit at 120s.</div>';
  });
  (d.warnings||[]).forEach(w=>{ban+='<div class=banner>'+esc(w)+'</div>';});
  $('#banners').innerHTML=ban;

  const bi=(d.baseline.items||[]);
  $('#base').innerHTML=bi.length
    ? bi.map(i=>'<span>'+esc(i.name)+' <b>'+gb(i.mb)+'</b>'+
        (i.hold_s?' <span class=hint>'+(i.kind==='other'
          ? 'assumed '+dur(i.hold_s)
          : 'keep-alive '+dur(i.hold_s))+'</span>':'')+'</span>').join('')
    : '<span class=hint>nothing resident — the whole board is free</span>';

  /* ---- grid ---- */
  let h='<div class=hdr><div>time</div><div>vram</div>'+
        LANES.map(l=>'<div>'+l[1]+'</div>').join('')+'</div>';

  let gut='';
  const t0=new Date(d.origin);
  for(let i=0;i<=d.hours;i++){
    const t=new Date(t0.getTime()+i*3600000);
    const mid=t.getHours()===0;
    gut+='<div class="hr'+(mid?' day':'')+'" style="top:'+(i*pxh)+'px">'+
      (mid?(t.getMonth()+1)+'/'+t.getDate():String(t.getHours()).padStart(2,'0')+':00')+'</div>';
  }
  h+='<div class="col gut" style="--h:'+H+'px">'+gut+'</div>';

  let sp='';
  d.slots.forEach(s=>{
    const r=s.mb/d.usable_mb;
    const cls=s.contended?'bad':(r>=.85?'mid':'ok');
    sp+='<div class="slot '+cls+'" title="'+hhmm(s.t)+'  '+gb(s.mb)+'" style="top:'+
      (s.off_s/3600*pxh+1)+'px;height:'+(d.slot_s/3600*pxh-2)+'px"></div>';
  });
  h+='<div class="col spine" style="--h:'+H+'px">'+sp+'</div>';

  LANES.forEach(([id,,cls])=>{
    const all=d.blocks.filter(b=>b.lane===id);

    /* A 15-minute watchdog is 96 identical zero-cost blocks a day. Drawn one by
       one it buries the handful of runs that actually matter, so anything that
       repeats this often collapses into a single cadence band. */
    const grp={};
    all.forEach(b=>{(grp[b.task]=grp[b.task]||[]).push(b);});
    const bands=[],loose=[];
    Object.keys(grp).forEach(t=>{
      const g=grp[t];
      if(g[0].ambient||g.length>DENSE){bands.push(g);}else{g.forEach(b=>loose.push(b));}
    });
    pack(loose);
    const lcols=loose.length?Math.max(...loose.map(b=>b._n)):0;
    const cols=bands.length+lcols||1;
    const cw=100/cols;
    let body='';

    bands.forEach((g,i)=>{
      const b=g[0], amb=b.ambient;
      let gap=null;
      if(!amb&&g.length>1){
        const ds=g.slice(1).map((x,j)=>x.off_s-g[j].off_s).sort((a,z)=>a-z);
        gap=ds[Math.floor(ds.length/2)];
      }
      body+='<div class="blk '+cls+' band'+(b.running?' run':'')+(SEL===b.id?' sel':'')+
        '" data-id="'+esc(b.id)+'" style="top:0;height:'+H+'px;left:calc('+(i*cw)+'% + 2px);width:calc('+cw+'% - 4px)">'+
        '<span class=n>'+esc(b.task)+(b.peak_mb?'<span class=chip>'+gb(b.peak_mb)+'</span>':'')+'</span>'+
        '<span class=m>'+(amb?'continuous':'every '+dur(gap)+' · '+g.length+'&times;')+'</span></div>';
    });

    loose.forEach(b=>{
      const top=Math.max(0,b.off_s/3600*pxh);
      const hgt=Math.max(17,b.dur_s/3600*pxh);
      const left=(bands.length+b._c)*cw;
      body+='<div class="blk '+cls+(b.running?' run':'')+(SEL===b.id?' sel':'')+
        '" data-id="'+esc(b.id)+'" style="top:'+top+'px;height:'+hgt+'px;left:calc('+left+'% + 2px);width:calc('+cw+'% - 4px)">'+
        '<span class=n>'+esc(b.task)+(b.peak_mb?'<span class=chip>'+gb(b.peak_mb)+'</span>':'')+'</span>'+
        (hgt>28?'<span class=m>'+hhmm(b.start)+' · '+dur(b.dur_s)+'</span>':'')+
        '</div>';
    });
    h+='<div class="col lanes" style="--h:'+H+'px">'+body+'</div>';
  });
  $('#grid').innerHTML=h;

  const nowTop=d.now_off_s/3600*pxh;
  $('#grid').querySelectorAll('.col.lanes').forEach(c=>{
    const n=document.createElement('div');n.className='now';n.style.top=nowTop+'px';c.appendChild(n);
  });

  /* ---- phone list ---- */
  const cnt={};
  d.blocks.forEach(b=>{cnt[b.task]=(cnt[b.task]||0)+1;});
  const shown={};
  const up=d.blocks.filter(b=>!b.ambient&&b.off_s+b.dur_s>=d.now_off_s)
                   .sort((a,b)=>a.off_s-b.off_s)
                   /* same rule as the grid: a 15-min watchdog gets one row, not 96 */
                   .filter(b=>cnt[b.task]<=DENSE||!shown[b.task]&&(shown[b.task]=1));
  const tight=new Set(d.slots.filter(s=>s.contended).flatMap(s=>s.jobs));
  $('#list').innerHTML=(up.length?up:[]).map(b=>
    '<div class="li'+(tight.has(b.task)?' bad':'')+'" data-id="'+esc(b.id)+'">'+
    '<div class=top><span class=tm>'+hhmm(b.start)+'</span>'+
    '<span class=nm>'+esc(b.task)+(cnt[b.task]>DENSE?' <span class=hint>&times;'+cnt[b.task]+'</span>':'')+'</span>'+
    '<span class=mb>'+dur(b.dur_s)+(b.peak_mb?' · '+gb(b.peak_mb):'')+'</span></div>'+
    '<div class=d>'+esc(b.desc||'').slice(0,110)+'</div></div>').join('')
    ||'<p class=hint style="padding:12px">Nothing scheduled in this window.</p>';

  document.querySelectorAll('.blk,.li').forEach(el=>
    el.onclick=()=>{SEL=el.dataset.id;detail();render(DATA);});

  $('#foot').textContent='updated '+d.ts+' · '+d.hours+'h window · 30-min slots';
  detail();
}

function detail(){
  const b=DATA&&DATA.blocks.find(x=>x.id===SEL);
  if(!b){$('#det').innerHTML='<p class=hint>Pick a block to see what it does.</p>';return;}
  const m=b.models.map(x=>esc(x.name)+' <span class=chip>'+gb(x.mb)+'</span>').join(' + ')
        ||'<span class=hint>no model — CPU / network only</span>';
  const conf=b.dur_src==='measured'
      ? 'measured, n='+b.dur_n+(b.dur_lo!==b.dur_hi?' ('+dur(b.dur_lo)+'–'+dur(b.dur_hi)+')':'')
      : 'estimated'+(b.dur_lo!==b.dur_hi?' ('+dur(b.dur_lo)+'–'+dur(b.dur_hi)+')':'')+', no runs observed yet';
  $('#det').innerHTML=
    '<p class=t>'+esc(b.task)+(b.running?' <span style="color:var(--grn)">● running</span>':'')+'</p>'+
    '<div class=meta>'+
      (b.ambient?'<span>starts at logon, <b>continuous</b></span>'
               :'<span><b>'+hhmm(b.start)+'</b>'+
                  (hhmm(b.end)!==hhmm(b.start)?' → '+hhmm(b.end):'')+'</span>'+
                '<span>takes <b>'+dur(b.dur_s)+'</b> <span class=hint>('+conf+')</span></span>')+
      '<span>holds <b>'+gb(b.peak_mb)+'</b></span>'+
      (b.limit_s?'<span>kill after '+dur(b.limit_s)+'</span>':'')+
      (b.last_run?'<span>last run '+esc(b.last_run)+
        (b.last_result&&b.last_result!=='0'?' <b class=bad>rc='+esc(b.last_result)+'</b>':'')+'</span>':'')+
    '</div>'+
    (b.desc?'<p>'+esc(b.desc)+'</p>':'')+
    '<p>'+m+'</p>'+
    (b.note?'<p class=note>'+esc(b.note)+'</p>':'')+
    (b.action?'<p class=cmd>'+esc(b.action)+'</p>':'');
}

async function tick(){
  try{
    const r=await fetch('/api/schedule?hours='+WIN);
    const d=await r.json();
    if(!d.ok){$('#foot').textContent=d.msg||'waiting for task data…';return;}
    DATA=d;render(d);
  }catch(e){$('#foot').textContent='disconnected — '+e;}
}
tick();setInterval(tick,30000);
</script></body></html>"""


# ---------------------------------------------------------------- cli
def _dump(hours, now):
    n = refresh_definitions()
    _load_hist()
    poll_status(verbose=True)
    f = forecast(hours, now)
    print(f"{n} tracked tasks · window {f['origin']} +{hours}h "
          f"· usable {f['usable_mb']}MB · baseline {f['baseline']['mb']}MB")
    for w in f["warnings"]:
        print(f"  ! {w}")
    print()
    per = {}
    for b in f["blocks"]:
        per.setdefault(b["task"], []).append(b)
    for task, bs in sorted(per.items()):
        b = bs[0]
        when = "continuous (ambient)" if b["ambient"] else \
            ", ".join(x["start"][11:16] for x in bs[:8]) + (" …" if len(bs) > 8 else "")
        print(f"{task:<32} {len(bs):>3} x  {b['peak_mb']:>6}MB  "
              f"{b['dur_s']:>6.0f}s [{b['dur_src']}]  {when}")
    print()
    hot = [s for s in f["slots"] if s["contended"]]
    print(f"peak {f['peak_mb']}MB · {len(hot)} contended slot(s)")
    for s in hot[:12]:
        print(f"  {s['t'][11:]}  {s['mb']:>6}MB  {' + '.join(s['jobs'])}")
    if f["next"]:
        print(f"\nnext: {f['next']['task']} in {f['next']['in_s']:.0f}s "
              f"({f['next']['peak_mb']}MB)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--hours", type=int, default=24, choices=(6, 12, 24))
    ap.add_argument("--now", help="pretend it is this local time, e.g. 2026-09-16T06:00")
    a = ap.parse_args()
    _dump(a.hours, datetime.fromisoformat(a.now) if a.now else None)
