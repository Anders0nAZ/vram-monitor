#!/usr/bin/env python3
"""Per-process VRAM on Windows, and what each process actually is.

The rest of this project infers non-Ollama VRAM as (board_used - ollama_vram)
and labels the residual "other (ComfyUI)". That is a guess, and on this machine
it is usually the wrong one: measured 2026-09-14, the 6.8GB residual was 5.0GB of
Ollama's OWN engine overhead - the --mmproj vision projector and the
--spec-type draft-mtp draft cache, neither of which /api/ps reports - roughly
0.7GB of desktop compositor and browsers, and 0.0GB of ComfyUI.

nvidia-smi genuinely cannot break this down: under Windows' WDDM driver model
--query-compute-apps returns [N/A] for used_gpu_memory on every process. But the
OS knows. Task Manager's per-process GPU memory column comes from PDH performance
counters, and those work fine:

    \\GPU Process Memory(pid_<pid>_luid_..)\\Local Usage

"Local Usage" is the one to read. "Dedicated Usage" double-counts surfaces a
process shares with the compositor - dwm.exe reports 9.1GB dedicated against
0.4GB local, and the dedicated figures sum to well over the board's capacity.
Local Usage summed across processes lands within ~1GB of nvidia-smi's board
total, the remainder being driver context that belongs to no process.

Stdlib only: ctypes against pdh.dll and kernel32, no subprocess, ~190ms a sample.
"""

import ctypes
import os
import threading
import time
from ctypes import wintypes

WINDOWS = os.name == "nt"

PDH_FMT_LARGE = 0x00000400
PDH_MORE_DATA = 0x800007D2
COUNTER = r"\GPU Process Memory(*)\Local Usage"
MB = 1024 * 1024

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_lock = threading.Lock()
_last = {"at": 0.0, "procs": [], "total_mb": 0, "ok": False}
_names = {}          # pid -> (name, path); processes are stable, so cache them
_names_at = 0.0


# ---------------------------------------------------------------- ctypes setup
class _VAL(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("largeValue", ctypes.c_longlong)]


class _ITEM(ctypes.Structure):
    _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _VAL)]


class _PROCENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


if WINDOWS:
    try:
        _pdh = ctypes.WinDLL("pdh.dll")
        _k32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

        _pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                       ctypes.POINTER(wintypes.HANDLE)]
        _pdh.PdhAddEnglishCounterW.argtypes = [
            wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_void_p,
            ctypes.POINTER(wintypes.HANDLE)]
        _pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
        _pdh.PdhGetFormattedCounterArrayW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        _pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
        for _f in (_pdh.PdhOpenQueryW, _pdh.PdhAddEnglishCounterW,
                   _pdh.PdhCollectQueryData, _pdh.PdhGetFormattedCounterArrayW,
                   _pdh.PdhCloseQuery):
            _f.restype = ctypes.c_long

        _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        _k32.OpenProcess.restype = wintypes.HANDLE
        _k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        _k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        _k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        _k32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                         ctypes.POINTER(_PROCENTRY32W)]
        _k32.Process32NextW.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(_PROCENTRY32W)]
        _AVAILABLE = True
    except (OSError, AttributeError):
        _AVAILABLE = False
else:
    _AVAILABLE = False


def available():
    return _AVAILABLE


# ---------------------------------------------------------------- identity
def _snapshot_names():
    """pid -> image name for every process.

    Toolhelp needs no handle, so unlike QueryFullProcessImageName it also names
    protected processes (dwm.exe, csrss.exe) that OpenProcess refuses.
    """
    out = {}
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return out
    try:
        e = _PROCENTRY32W()
        e.dwSize = ctypes.sizeof(_PROCENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = e.szExeFile
            ok = _k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return out


def _full_path(pid):
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None                       # protected process - name only
    try:
        n = wintypes.DWORD(32768)
        b = ctypes.create_unicode_buffer(32768)
        if _k32.QueryFullProcessImageNameW(h, 0, b, ctypes.byref(n)):
            return b.value
    finally:
        _k32.CloseHandle(h)
    return None


def _identify(pids):
    """Resolve pids to (name, path), refreshing the snapshot at most every 20s."""
    global _names_at
    now = time.monotonic()
    unknown = [p for p in pids if p not in _names]
    if unknown or now - _names_at > 20:
        snap = _snapshot_names()
        _names_at = now
        for p in pids:
            if p in _names and p in snap:
                continue
            _names[p] = (snap.get(p) or f"pid {p}", _full_path(p))
        # drop cache entries for processes that have exited
        for dead in [p for p in _names if p not in snap]:
            _names.pop(dead, None)
    return {p: _names.get(p, (f"pid {p}", None)) for p in pids}


# ---------------------------------------------------------------- sampling
def _read_counter():
    q = wintypes.HANDLE()
    if _pdh.PdhOpenQueryW(None, None, ctypes.byref(q)) != 0:
        return None
    try:
        c = wintypes.HANDLE()
        if _pdh.PdhAddEnglishCounterW(q, COUNTER, None, ctypes.byref(c)) != 0:
            return None
        # Two collections: the first enumerates the wildcard's instances.
        if _pdh.PdhCollectQueryData(q) != 0:
            return None
        _pdh.PdhCollectQueryData(q)
        size = wintypes.DWORD(0)
        count = wintypes.DWORD(0)
        rc = _pdh.PdhGetFormattedCounterArrayW(
            c, PDH_FMT_LARGE, ctypes.byref(size), ctypes.byref(count), None)
        # PDH returns signed longs; the status constants are unsigned.
        if (rc & 0xFFFFFFFF) != PDH_MORE_DATA or not count.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if _pdh.PdhGetFormattedCounterArrayW(
                c, PDH_FMT_LARGE, ctypes.byref(size), ctypes.byref(count), buf) != 0:
            return None
        items = ctypes.cast(buf, ctypes.POINTER(_ITEM))
        out = []
        for i in range(count.value):
            inst, val = items[i].szName, items[i].FmtValue.largeValue
            if not inst or not inst.startswith("pid_") or val <= 0:
                continue
            try:
                pid = int(inst.split("_")[1])
            except (IndexError, ValueError):
                continue
            out.append((pid, int(val)))
        return out
    finally:
        _pdh.PdhCloseQuery(q)


def sample(min_mb=8):
    """[{pid, name, path, mb}] sorted big-first, plus the attributed total."""
    if not _AVAILABLE:
        return {"ok": False, "procs": [], "total_mb": 0}
    try:
        rows = _read_counter()
    except OSError:
        rows = None
    if rows is None:
        return {"ok": False, "procs": [], "total_mb": 0}

    # One process can hold several adapter instances; charge them together.
    by_pid = {}
    for pid, val in rows:
        by_pid[pid] = by_pid.get(pid, 0) + val
    ident = _identify(list(by_pid))
    procs = []
    for pid, val in by_pid.items():
        mb = val // MB
        if mb < min_mb:
            continue
        name, path = ident.get(pid, (f"pid {pid}", None))
        procs.append({"pid": pid, "name": name, "path": path, "mb": mb})
    procs.sort(key=lambda p: -p["mb"])
    return {"ok": True, "procs": procs,
            "total_mb": sum(v for v in by_pid.values()) // MB}


def latest(max_age=8.0):
    """Cached sample. The PDH round trip is ~190ms, too slow for a 2s poll."""
    with _lock:
        if _last["ok"] and time.monotonic() - _last["at"] < max_age:
            return dict(_last)
    s = sample()
    s["at"] = time.monotonic()
    with _lock:
        _last.clear()
        _last.update(s)
    return dict(s)


if __name__ == "__main__":
    s = sample(min_mb=1)
    print(f"available={available()} ok={s['ok']} attributed={s['total_mb']}MB")
    for p in s["procs"]:
        print(f"  {p['mb']:>7} MB  {p['name']:<22} {p['path'] or ''}")
