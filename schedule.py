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

# Chart granularity, per window. Finer slots on a short window keep a 20-second
# job from being drawn as though it occupied half an hour.
SLOT_MINUTES   = 30       # fallback / origin rounding
SLOT_FOR_HOURS = {6: 10, 12: 15, 24: 30}
MAX_OCC        = 400      # per-trigger runaway guard
HIST_KEEP      = 20       # observed durations retained per task
MIN_SAMPLES    = 3        # before a learned median beats the seed
# Estimate from the most recent runs only. These durations drift with regime, not
# just noise: RobonerRefresh ran 12-48s through August and 207-5005s from the
# first week of the season, because scout went from a quiet news pool to a full
# one. A flat median over 20 samples would have reported 47s for a job that now
# takes half an hour - and it is the job that causes the contention.
MEDIAN_WINDOW  = 8
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


def process_rules():
    """Name-a-process rules, read by vram_monitor's VRAM attribution."""
    return _manifest().get("processes", [])


def loaders_of(model):
    """Which jobs and daemons declare this model - i.e. who would have loaded it.

    jobs.json already says which models each job pulls in, so the reverse index
    answers "what put this on the GPU" without any extra bookkeeping.
    """
    key = _norm_model(model)
    out = []
    m = _manifest()
    for entry in m.get("jobs", []):
        if key in [_norm_model(x) for x in (entry.get("models") or [])]:
            out.append({"name": entry.get("match"), "kind": "job"})
    for entry in m.get("daemons", []):
        if key in [_norm_model(x) for x in (entry.get("models") or [])]:
            out.append({"name": entry.get("label") or entry.get("id"),
                        "kind": "daemon"})
    return out


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


def _is_model(name):
    """Real model names always carry a :tag. The baseline residual does not."""
    return bool(name) and ":" in name and not name.startswith("other")


def _palette_order():
    """Stable colour order for models, derived from jobs.json alone.

    Colour must follow the entity, not its rank in whatever happens to be on
    screen. Indexing into the models present in the current window would repaint
    every survivor each time the 6/12/24h toggle changed the set - qwen3.8 blue
    at 6h and green at 24h. This order depends only on the manifest, so a model
    keeps its hue across windows, across days, and across restarts.
    """
    m = _manifest()
    out = []
    for group in ("jobs", "daemons"):
        for entry in m.get(group, []):
            for name in (entry.get("models") or []):
                key = _norm_model(name)
                if key and key not in out:
                    out.append(key)
    return out


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


def hist_key(name, prof=None):
    """Which history bucket a task's runs belong in.

    A regex-matched profile covers a family of tasks that are really the same job
    under generated names - NFLModelCapture_20260914_1705 and tomorrow's
    equivalent. Keyed by task name each would hold exactly one run and never reach
    MIN_SAMPLES, so the family shares its profile's pattern as the key.
    """
    prof = prof if prof is not None else _profile(name)
    if prof and prof.get("regex"):
        return prof.get("match") or name
    return name


def _record_duration(name, seconds):
    """Observed wall clock for one run. Median, not high-water: unlike VRAM cost,
    over-estimating a duration is not the safe direction - it would paint
    phantom collisions across the calendar."""
    global _hist_dirty
    if seconds is None or seconds < 0:
        return
    h = _hist.setdefault(hist_key(name), {"samples": []})
    h["samples"] = (h.get("samples") or [])[-(HIST_KEEP - 1):] + [int(seconds)]
    h["last_dur"] = int(seconds)
    h["last_run"] = datetime.now().isoformat(timespec="seconds")
    _hist_dirty = True


def _duration_for(name, prof):
    """(seconds, source, n, lo, hi) - learned median once there is enough of it."""
    h = _hist.get(hist_key(name, prof)) or {}
    samples = h.get("samples") or []
    seed = int((prof or {}).get("seed_seconds", 60) or 0)
    seed_hi = (prof or {}).get("seed_max_seconds")
    if len(samples) >= MIN_SAMPLES:
        recent = samples[-MEDIAN_WINDOW:]
        return (int(_median(recent)), "measured", len(samples),
                min(recent), max(recent))
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
                return (int(b["total_mb"]), b.get("used_mb"), b.get("other_mb"),
                        b.get("attrib"))
        except Exception:
            pass
    return FALLBACK_TOTAL_MB, None, None, None


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
                      "hold_s": max(0.0, hold), "kind": "model",
                      "by": loaders_of(name)})

    # Non-Ollama VRAM, named. attribute_vram() reads the OS per-process counters,
    # so this is measured rather than the old "other (ComfyUI / unattributed)"
    # residual - which on this box was mostly Ollama's own engine overhead while
    # ComfyUI sat at zero.
    _, _, other, attrib = _board()
    if attrib and attrib.get("ok"):
        for part in attrib.get("parts", []):
            mb = int(part.get("mb") or 0)
            if mb < BASELINE_FLOOR_MB:
                continue
            items.append({"name": part["label"], "mb": mb,
                          "hold_s": float(OTHER_HOLD_SECONDS),
                          "kind": part.get("group", "other"),
                          "procs": part.get("procs", [])})
    elif other and other >= BASELINE_FLOOR_MB:
        items.append({"name": "unattributed", "mb": int(other),
                      "hold_s": float(OTHER_HOLD_SECONDS), "kind": "other"})
    return {"mb": sum(i["mb"] for i in items), "items": items,
            "other_hold_s": OTHER_HOLD_SECONDS,
            "measured": bool(attrib and attrib.get("ok"))}


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

    total_mb, used_mb, other_mb, _ = _board()
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

    # --- rollup. A model held by two concurrent jobs is ONE copy in VRAM, so slot
    # cost is the union of model names, not the sum of per-job peaks. The chart
    # plots VRAM on the y-axis, and what occupies VRAM is models - so each slot
    # carries its per-model breakdown, which is what gets stacked.
    slots, collisions = [], []
    step = SLOT_FOR_HOURS.get(hours, SLOT_MINUTES) * 60
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
                      "parts": dict(sizes),
                      "held": sorted(set(held)),
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

    # --- stacking series. Colour is assigned by model NAME, not by size or rank,
    # so a model keeps its hue when the set around it changes. "other" is the
    # unattributed residual, not an identity, so it takes the neutral and always
    # sits at the bottom of the stack.
    names = {n for s in slots for n in s["parts"]}
    other = sorted(n for n in names if not _is_model(n))
    models = sorted(n for n in names if _is_model(n))
    universe = _palette_order()
    # Overhead holders (Ollama's engine, the compositor, the driver reserve) are
    # not identities competing with the models, so they take a neutral ramp rather
    # than categorical hues - but they still need to be told apart, which one flat
    # grey for all of them did not do.
    series = [{"name": n, "slot": i % 3, "kind": "other"}
              for i, n in enumerate(other)]
    series += [{"name": n,
                "slot": (universe.index(n) if n in universe
                         else len(universe) + sorted(names).index(n)) % 6,
                "kind": "model", "by": loaders_of(n)} for n in models]
    for s in series:
        s["peak_mb"] = max((sl["parts"].get(s["name"], 0) for sl in slots), default=0)
        s.setdefault("by", [])

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
        "series": series,
        "slot_minutes": step // 60,
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
--grn:#3fb950;--amb:#d29922;--red:#f85149;--blu:#58a6ff;
/* Categorical slots: the validated dark-mode palette, checked against this
   card surface (#161b22) - worst adjacent CVD dE 8.4, all six >=3:1 contrast.
   Assigned by model NAME so a model keeps its hue as the set changes. */
--s0:#3987e5;--s1:#d95926;--s2:#199e70;--s3:#c98500;--s4:#d55181;--s5:#9085e9;
/* Neutral ramp for overhead holders - not identities, so no hue. Only these
   three steps clear 3:1 on this surface (8.2 / 5.6 / 3.8); darker greys fail. */
--n0:#aab4bf;--n1:#8b949e;--n2:#6e7681;
--crit:#d03b3b;--warnc:#fab219;        /* status - never reused for a series */
--grid:#21262d;--plot-h:280px;--gut:86px;--row-h:19px}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:14px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:15px;margin:0 0 12px;display:flex;justify-content:space-between;
align-items:baseline;gap:12px;flex-wrap:wrap}
h1 .gpu{color:var(--mut);font-weight:400}
a{color:var(--blu);text-decoration:none}a:hover{text-decoration:underline}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px;margin-bottom:12px}
.btns{display:flex;gap:6px}
button{font:inherit;font-size:12.5px;color:var(--fg);background:#21262d;border:1px solid var(--bd);
border-radius:7px;padding:6px 12px;cursor:pointer}
button:hover{border-color:var(--blu)}
button.on{border-color:var(--blu);color:var(--blu)}
.sum{color:var(--mut);font-size:12.5px;display:flex;gap:18px;flex-wrap:wrap}
.sum b{color:var(--fg);font-weight:600}
.sum b.bad{color:var(--crit)}.sum b.warn{color:var(--amb)}
.banner{background:#3a2a05;border:1px solid var(--amb);color:var(--amb);border-radius:7px;
padding:9px 11px;margin-bottom:12px;font-size:12.5px}
.banner.bad{background:#3d1417;border-color:var(--crit);color:#ff9a9a}
.lbl{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.06em;margin:0 0 8px}
.hint{color:var(--mut)}

/* ---- legend: identity is never colour-alone, so every swatch carries its name ---- */
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px;font-size:12px}
.lg{display:flex;align-items:center;gap:6px;color:var(--mut)}
.lg i{width:11px;height:11px;border-radius:3px;display:inline-block;flex:none}
.lg b{color:var(--fg);font-weight:600}

/* ---- chart ---- */
.chart{display:grid;grid-template-columns:var(--gut) minmax(0,1fr);gap:0}
.yax{position:relative;height:var(--plot-h)}
.yax span{position:absolute;right:8px;transform:translateY(-50%);color:var(--mut);font-size:11px;white-space:nowrap}
.plot{position:relative;height:var(--plot-h);border-left:1px solid var(--bd);
border-bottom:1px solid var(--bd);overflow:hidden}
.gl{position:absolute;left:0;right:0;height:1px;background:var(--grid)}
.over{position:absolute;left:0;right:0;top:0;background:rgba(208,59,59,.10)}
.thresh{position:absolute;left:0;right:0;height:0;border-top:1px dashed var(--crit)}
.thresh:after{content:attr(data-l);position:absolute;right:3px;top:-15px;font-size:10px;
color:var(--crit);background:var(--card);padding:0 4px}
.col{position:absolute;bottom:0;top:0}
.seg{position:absolute;left:1px;right:1px;border-radius:2px}
.rug{position:absolute;top:0;height:3px;background:var(--crit)}
.nowl{position:absolute;top:0;bottom:0;width:0;border-left:1px solid var(--red);z-index:4}
.nowl:after{content:'now';position:absolute;left:3px;top:1px;font-size:10px;color:var(--red);
background:var(--card);padding:0 3px}
.xax{position:relative;height:20px;margin-left:var(--gut);margin-top:4px}
.xax span{position:absolute;transform:translateX(-50%);color:var(--mut);font-size:11px;white-space:nowrap}

/* ---- job strip: the old lanes, kept as rows on the same time axis ---- */
.strip{display:grid;grid-template-columns:var(--gut) minmax(0,1fr);
border-top:1px solid var(--grid);margin-top:10px;padding-top:8px}
.rowlbl{position:relative}
.rowlbl span{position:absolute;right:8px;
color:var(--mut);font-size:10.5px;white-space:nowrap;text-transform:uppercase;letter-spacing:.04em}
.rows{position:relative}
.row{position:relative;height:var(--row-h);border-bottom:1px solid #1b222c}
.row:last-child{border-bottom:0}
.jb{position:absolute;top:2px;height:calc(var(--row-h) - 5px);border-radius:3px;
min-width:3px;cursor:pointer;overflow:hidden;font-size:10px;line-height:14px;
padding:0 4px;white-space:nowrap;color:#0d1117;font-weight:600}
.jb:hover{filter:brightness(1.2)}
.jb.sel{outline:2px solid var(--fg);outline-offset:-1px;z-index:3}
.jb.cpu{background:#3d444d;color:var(--fg);font-weight:400}
.jb.amb{background:repeating-linear-gradient(45deg,#2a313c 0 6px,#232a34 6px 12px);
color:var(--mut);font-weight:400}
.nowr{position:absolute;top:0;bottom:0;width:0;border-left:1px solid var(--red);z-index:4}

/* ---- tooltip ---- */
.tip{position:fixed;z-index:50;background:#0d1117;border:1px solid var(--bd);border-radius:7px;
padding:8px 10px;font-size:12px;pointer-events:none;display:none;box-shadow:0 6px 20px rgba(0,0,0,.6);
max-width:290px}
.tip .h{font-weight:600;margin-bottom:5px}
.tip .r{display:flex;gap:8px;align-items:center;color:var(--mut)}
.tip .r i{width:9px;height:9px;border-radius:2px;flex:none}
.tip .r b{color:var(--fg);margin-left:auto;font-weight:600}
.tip .tot{border-top:1px solid var(--grid);margin-top:5px;padding-top:5px}

/* ---- upcoming table (also the table view for the chart) ---- */
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--mut);font-weight:400;font-size:11px;text-transform:uppercase;
letter-spacing:.06em;padding:0 8px 6px 0;border-bottom:1px solid var(--grid)}
td{padding:5px 8px 5px 0;border-top:1px solid var(--grid);vertical-align:top}
tr.j{cursor:pointer}tr.j:hover td{background:#1c2128}
tr.j.sel td{background:#1c2128;box-shadow:inset 2px 0 0 var(--blu)}
td.n{font-weight:600}
td.r{text-align:right;color:var(--mut);white-space:nowrap}
.sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;vertical-align:baseline}
.tight{color:var(--crit)}

.det{min-height:74px;position:sticky;bottom:10px;z-index:20;box-shadow:0 6px 22px rgba(0,0,0,.65)}
.det .t{font-size:14px;font-weight:600;margin:0 0 6px}
.det .meta{color:var(--mut);display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px;font-size:12px}
.det .meta b{color:var(--fg);font-weight:600}
.det p{margin:0 0 7px}
.det .note{color:var(--mut);border-left:2px solid var(--bd);padding-left:9px}
.det .cmd{color:var(--mut);font-size:11.5px;word-break:break-all}
.foot{color:var(--mut);font-size:11px;text-align:center;margin-top:4px}
@media(max-width:820px){
  :root{--plot-h:220px}
  body{padding:10px}
  .chart{grid-template-columns:42px minmax(0,1fr)}
  .xax,.cpu{margin-left:42px}
  td.hide,th.hide{display:none}
}
</style></head><body><div class=wrap>

<h1><span>Stack Schedule <span class=gpu id=gpu>&ndash;</span></span>
  <span class=btns>
    <button data-h=6 onclick="setWin(6)">6h</button>
    <button data-h=12 onclick="setWin(12)">12h</button>
    <button data-h=24 onclick="setWin(24)">24h</button>
    <a href="/" style="align-self:center;margin-left:6px">monitor &rarr;</a>
  </span></h1>

<div id=banners></div>

<div class=card>
  <div class=sum id=sum></div>
  <p class=lbl style="margin:11px 0 6px">held now &mdash; every job has to fit around this</p>
  <div class=sum id=base></div>
</div>

<div class=card>
  <div class=legend id=legend></div>
  <div class=chart>
    <div class=yax id=yax></div>
    <div class=plot id=plot></div>
  </div>
  <div class=xax id=xax></div>
  <div class=strip>
    <div class=rowlbl id=rowlbl></div>
    <div class=rows id=rows></div>
  </div>
</div>

<div class=card>
  <p class=lbl>upcoming runs</p>
  <div id=table></div>
</div>

<div class="card det" id=det><p class=hint>Pick a run to see what it does.</p></div>
<p class=foot id=foot>&ndash;</p>
</div>
<div class=tip id=tip></div>
<script>
const $=s=>document.querySelector(s);
const DENSE=5;
let WIN=+(localStorage.getItem('sched_win')||6), DATA=null, SEL=null;
if(![6,12,24].includes(WIN))WIN=6;

function gb(mb){return mb>=1024?(mb/1024).toFixed(1)+'GB':Math.round(mb)+'MB';}
function dur(s){s=Math.round(s);if(s<60)return s+'s';
  if(s<3600)return Math.round(s/60)+'m';
  const h=Math.floor(s/3600),m=Math.round((s%3600)/60);return m?h+'h'+m+'m':h+'h';}
function hhmm(iso){return iso?iso.slice(11,16):'';}
function esc(t){const d=document.createElement('div');d.textContent=t==null?'':t;return d.innerHTML;}
function setWin(h){WIN=h;localStorage.setItem('sched_win',h);tick();}

/* Colour follows the entity: the server assigns a slot per model name, so a
   model keeps its hue when other series appear or vanish. */
function colour(s){return s.kind==='other'?'var(--n'+s.slot+')':'var(--s'+s.slot+')';}

function render(d){
  const T=d.total_mb||24576, U=d.usable_mb, span=d.hours*3600;
  const byName={}; d.series.forEach(s=>byName[s.name]=s);
  const pct=mb=>mb/T*100;

  document.querySelectorAll('.btns button').forEach(b=>
    b.classList.toggle('on',+b.dataset.h===d.hours));
  $('#gpu').textContent=(d.used_mb!=null?gb(d.used_mb)+' / ':'')+gb(T)
    +' · '+gb(U)+' usable';

  /* ---- summary ---- */
  const bad=d.slots.filter(s=>s.contended).length;
  $('#sum').innerHTML=
    '<span>next <b>'+(d.next?esc(d.next.task)+'</b> in <b>'+dur(d.next.in_s):'—</b><b>')+'</b></span>'+
    '<span>baseline <b>'+gb(d.baseline.mb)+'</b></span>'+
    '<span>window peak <b class="'+(d.peak_mb>U?'bad':d.peak_mb>U*.85?'warn':'')+'">'+gb(d.peak_mb)+'</b></span>'+
    '<span>runs <b>'+d.blocks.filter(b=>!b.ambient).length+'</b></span>'+
    '<span>over capacity <b class="'+(bad?'bad':'')+'">'+bad+'</b> slot'+(bad===1?'':'s')+'</span>';

  const bi=(d.baseline.items||[]);
  $('#base').innerHTML=(bi.length
    ? bi.map(i=>{
        const who=i.kind==='model'
          ? (i.by&&i.by.length?i.by.map(x=>x.name).join(', '):'no declared loader')
          : (i.procs&&i.procs.length
              ? [...new Set(i.procs.map(p=>p.name))].slice(0,2).join(', ')
              : '');
        return '<span>'+esc(i.name)+' <b>'+gb(i.mb)+'</b>'+
          (who?' <span class=hint>'+esc(who)+'</span>':'')+
          (i.kind==='model'&&i.hold_s?' <span class=hint>keep-alive '+dur(i.hold_s)+'</span>':'')+
          '</span>';
      }).join('')
    : '<span class=hint>nothing resident — the whole board is free</span>')
    +(d.baseline.measured===false
      ? '<span class=hint>(per-process counters unavailable — residual only)</span>':'');

  /* ---- banners ---- */
  let ban='';
  (d.collisions||[]).slice(0,3).forEach(c=>{
    const who=c.jobs[0]==='baseline alone'
      ? 'Nothing scheduled is to blame — the '+gb(c.base_mb)+' already resident is itself over the line'
      : esc(c.jobs.join(' + '))+(c.base_mb?', on top of '+gb(c.base_mb)+' already held':'');
    ban+='<div class="banner bad">'+hhmm(c.t)+'–'+hhmm(c.until)+' — '+gb(c.mb)+
      ' projected vs '+gb(U)+' the gate can hand out. '+who+
      '. Expect a queue hold, then a forced admit at 120s.</div>';
  });
  (d.warnings||[]).forEach(w=>{ban+='<div class=banner>'+esc(w)+'</div>';});
  $('#banners').innerHTML=ban;

  /* ---- legend ---- */
  $('#legend').innerHTML=d.series.length
    ? d.series.map(s=>{
        const by=(s.by||[]).map(x=>x.name);
        const who=by.length===0?'':(by.length<=2?by.join(', '):by.length+' jobs');
        return '<span class=lg><i style="background:'+colour(s)+'"></i>'+
          esc(s.name)+' <b>'+gb(s.peak_mb)+'</b>'+
          (who?' <span class=hint>&larr; '+esc(who)+'</span>':'')+'</span>';
      }).join('')
      +'<span class=lg><i style="background:var(--crit);opacity:.5"></i>over capacity</span>'
    : '<span class=hint>no VRAM committed anywhere in this window</span>';

  /* ---- y axis: absolute VRAM, 0 to the board's real total ---- */
  let y='';
  for(let g=0;g<=Math.floor(T/1024);g+=4){
    y+='<span style="top:'+(100-pct(g*1024))+'%">'+g+'GB</span>';
  }
  $('#yax').innerHTML=y;

  /* ---- plot ---- */
  let p='';
  for(let g=0;g<=Math.floor(T/1024);g+=4){
    p+='<div class=gl style="top:'+(100-pct(g*1024))+'%"></div>';
  }
  p+='<div class=over style="height:'+pct(T-U)+'%"></div>';
  p+='<div class=thresh data-l="'+gb(U)+' usable" style="top:'+pct(T-U)+'%"></div>';

  const n=d.slots.length, cw=100/n;
  d.slots.forEach((s,i)=>{
    let acc=0, segs='';
    /* Stack in server series order so bands line up across columns and read as
       one continuous area rather than a shuffled bar chart. */
    d.series.forEach(se=>{
      const mb=s.parts[se.name]; if(!mb)return;
      const h=pct(mb);
      segs+='<div class=seg style="bottom:'+pct(acc)+'%;height:calc('+h+'% - 2px);'+
            'background:'+colour(se)+'"></div>';
      acc+=mb;
    });
    if(segs)p+='<div class=col data-i="'+i+'" style="left:'+(i*cw)+'%;width:'+cw+'%">'+segs+'</div>';
    if(s.contended)p+='<div class=rug style="left:'+(i*cw)+'%;width:'+cw+'%"></div>';
  });
  p+='<div class=nowl style="left:'+(d.now_off_s/span*100)+'%"></div>';
  $('#plot').innerHTML=p;

  /* ---- x axis: ticks on real hour boundaries, ~8 of them.
         The origin is a slot boundary (e.g. 09:50), so stepping from it and
         printing the hour would label that tick "09:00" - an hour adrift. ---- */
  const t0=new Date(d.origin), stepH=Math.max(1,Math.round(d.hours/8));
  const first=new Date(t0); first.setMinutes(0,0,0);
  if(first<t0)first.setHours(first.getHours()+1);
  while(first.getHours()%stepH!==0)first.setHours(first.getHours()+1);
  let x='';
  for(let t=new Date(first);(t-t0)/3600000<=d.hours;t.setHours(t.getHours()+stepH)){
    const off=(t-t0)/1000, mid=t.getHours()===0;
    x+='<span style="left:'+(off/span*100)+'%">'+
       (mid?(t.getMonth()+1)+'/'+t.getDate():String(t.getHours()).padStart(2,'0')+':00')+'</span>';
  }
  $('#xax').innerHTML=x;

  /* ---- job strip: a run has no height on a VRAM axis (a CPU job has none at
         all), so the lanes live on as rows sharing this chart's time axis ---- */
  const cnt={}; d.blocks.forEach(b=>{cnt[b.task]=(cnt[b.task]||0)+1;});
  const LANES=(d.lanes&&d.lanes.length?d.lanes:[
    {id:'gpu-heavy',label:'GPU heavy'},{id:'gpu-light',label:'GPU light'},
    {id:'cpu',label:'CPU / net'},{id:'always-on',label:'always on'}]);
  let lb='',rw='',top=0;
  LANES.forEach(ln=>{
    const mine=d.blocks.filter(b=>b.lane===ln.id);
    /* A 15-minute watchdog is 97 slivers in a 19px row, so it collapses to one
       labelled band. Those bands and the continuous jobs are all full-width, so
       two in a lane would sit exactly on top of each other - they get a sub-lane
       each and the row grows to fit. */
    const full=[],timed=[],seen={};
    mine.forEach(b=>{
      const dense=cnt[b.task]>DENSE&&!b.ambient;
      if((dense||b.ambient)&&seen[b.task])return;
      if(dense||b.ambient){seen[b.task]=1;full.push([b,dense]);}else timed.push(b);
    });
    const sub=Math.max(1,full.length);
    const rh=Math.max(19,sub*13+4);
    lb+='<span style="top:'+top+'px;height:'+rh+'px;line-height:'+rh+'px">'+esc(ln.label)+'</span>';
    top+=rh;
    let bars='';
    full.forEach(([b,dense],i)=>{
      const h=(rh-4)/sub;
      bars+='<div class="jb '+(b.ambient?'amb':'cpu')+(SEL===b.id?' sel':'')+
        '" data-id="'+esc(b.id)+'" style="left:0;width:100%;top:'+(2+i*h)+
        'px;height:'+(h-1)+'px;line-height:'+(h-1)+'px" title="'+esc(b.task)+
        (b.ambient?' (continuous)':' ×'+cnt[b.task])+'">'+
        esc(b.task)+(dense?' ×'+cnt[b.task]:'')+'</div>';
    });
    timed.forEach(b=>{
      const se=b.models.length?byName[b.models[0].name]:null;
      const w=Math.max(0.35,b.dur_s/span*100);
      bars+='<div class="jb '+(b.models.length?'':'cpu')+(SEL===b.id?' sel':'')+
        '" data-id="'+esc(b.id)+'" style="left:'+(b.off_s/span*100)+'%;width:'+w+'%;'+
        (se?'background:'+colour(se):'')+'" title="'+esc(b.task)+' '+hhmm(b.start)+
        ' · '+dur(b.dur_s)+(b.peak_mb?' · '+gb(b.peak_mb):'')+'">'+
        (w>6?esc(b.task):'')+'</div>';
    });
    rw+='<div class=row style="height:'+rh+'px">'+bars+'</div>';
  });
  $('#rowlbl').innerHTML=lb;
  $('#rows').innerHTML=rw+'<div class=nowr style="left:'+(d.now_off_s/span*100)+'%"></div>';

  /* ---- upcoming runs: the per-job detail, and the chart's table view ---- */
  const shown={};
  const up=d.blocks.filter(b=>!b.ambient&&b.off_s+b.dur_s>=d.now_off_s)
    .sort((a,b)=>a.off_s-b.off_s)
    .filter(b=>cnt[b.task]<=DENSE||(!shown[b.task]&&(shown[b.task]=1)));
  const tight=new Set(d.slots.filter(s=>s.contended).flatMap(s=>s.jobs));
  $('#table').innerHTML=up.length
    ? '<table><thead><tr><th>time</th><th>job</th><th>holds</th>'+
      '<th class=hide>for</th><th class="hide">what it does</th></tr></thead><tbody>'+
      up.map(b=>{
        const sw=b.models.map(m=>{const s=byName[m.name];
          return s?'<i class=sw style="background:'+colour(s)+'"></i>':'';}).join('');
        return '<tr class="j'+(SEL===b.id?' sel':'')+'" data-id="'+esc(b.id)+'">'+
          '<td class=r>'+hhmm(b.start)+'</td>'+
          '<td class="n'+(tight.has(b.task)?' tight':'')+'">'+sw+esc(b.task)+
            (cnt[b.task]>DENSE?' <span class=hint>&times;'+cnt[b.task]+'</span>':'')+'</td>'+
          '<td class=r>'+(b.peak_mb?gb(b.peak_mb):'—')+'</td>'+
          '<td class="r hide">'+dur(b.dur_s)+'</td>'+
          '<td class=hide>'+esc((b.desc||'').slice(0,80))+'</td></tr>';
      }).join('')+'</tbody></table>'
    : '<p class=hint>Nothing scheduled in this window.</p>';

  document.querySelectorAll('tr.j,.jb').forEach(el=>
    el.onclick=()=>{SEL=el.dataset.id;render(DATA);});

  bindTip(d);
  $('#foot').textContent='updated '+d.ts+' · '+d.hours+'h window · '+
    d.slot_minutes+'-min slots';
  detail();
}

/* ---- hover layer: an area chart without one is a picture, not a chart ---- */
function bindTip(d){
  const plot=$('#plot'), tip=$('#tip');
  plot.onmousemove=e=>{
    const r=plot.getBoundingClientRect();
    const i=Math.min(d.slots.length-1,Math.max(0,
      Math.floor((e.clientX-r.left)/r.width*d.slots.length)));
    const s=d.slots[i]; if(!s){tip.style.display='none';return;}
    const rows=d.series.filter(se=>s.parts[se.name]).map(se=>{
      const by=(se.by||[]).map(x=>x.name).join(', ');
      return '<div class=r><i style="background:'+colour(se)+'"></i>'+esc(se.name)+
        '<b>'+gb(s.parts[se.name])+'</b></div>'+
        (by?'<div class=r style="padding-left:17px;font-size:11px">&larr; '+esc(by)+'</div>':'');
    }).join('')||'<div class=r>nothing resident</div>';
    const end=new Date(new Date(s.t).getTime()+d.slot_minutes*60000);
    tip.innerHTML='<div class=h>'+hhmm(s.t)+'–'+
      String(end.getHours()).padStart(2,'0')+':'+String(end.getMinutes()).padStart(2,'0')+
      '</div>'+rows+
      '<div class="r tot">total<b class="'+(s.contended?'tight':'')+'">'+gb(s.mb)+'</b></div>'+
      (s.contended?'<div class=r style="color:var(--crit)">over the '+gb(d.usable_mb)+' limit</div>':'')+
      (s.jobs.length?'<div class=r style="margin-top:4px">'+esc(s.jobs.join(', '))+'</div>':'');
    tip.style.display='block';
    const tw=tip.offsetWidth, th=tip.offsetHeight;
    tip.style.left=Math.min(window.innerWidth-tw-8,Math.max(8,e.clientX+14))+'px';
    tip.style.top=Math.max(8,e.clientY-th-12)+'px';
  };
  plot.onmouseleave=()=>{$('#tip').style.display='none';};
}

function detail(){
  const b=DATA&&DATA.blocks.find(x=>x.id===SEL);
  if(!b){$('#det').innerHTML='<p class=hint>Pick a run to see what it does.</p>';return;}
  const m=b.models.map(x=>esc(x.name)+' <span class=hint>'+gb(x.mb)+'</span>').join(' + ')
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
        (b.last_result&&b.last_result!=='0'?' <b class=tight>rc='+esc(b.last_result)+'</b>':'')+'</span>':'')+
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
