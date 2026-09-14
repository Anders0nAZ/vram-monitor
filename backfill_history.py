#!/usr/bin/env python3
"""Seed job-history.json from the projects' own logs.

The status poller can only learn a job's duration by watching it run, so a fresh
install knows nothing and a job that runs weekly takes a month to characterise.
The projects have been logging for far longer, so where a log records the same
quantity the poller measures - wall clock for one whole run - it can be read back.

"Where" is the whole difficulty. Of the four logs examined, exactly one qualifies:

  refresh.log      USED. "=== refresh start ===" / "=== refresh done ===" bracket
                   the run with real timestamps. Validated against the poller: the
                   log gives 5005s for 2026-09-10, the poller independently
                   measured 4994s on the same run - agreement within 11s.

  sync.log         REFUSED. Its bracketed timestamps are a single stamp reused for
                   every line of the run: start and end both read 02:00:01, giving
                   0s, while the body of the same run reports 123.9s of embedding.
                   The stamps do not measure anything.

  inseason.log     REFUSED. Cascade blocks carry per-step durations that can be
                   summed, but that is the cascade's internal work, not the task's
                   wall clock - it excludes the wscript wrapper, the interpreter
                   start and the imports. Step-sums median 30s against the poller's
                   measured 155s for the same task. At this scale the missing part
                   is most of the number.

  capture.log      REFUSED. No timestamps of any kind.

A wrong duration is worse than no duration: it is what decides whether the
calendar claims a slot fits. So the refusals stay refusals rather than being
patched up with an assumed startup offset.

    python backfill_history.py              # show what it would do
    python backfill_history.py --apply      # write job-history.json
"""

import argparse
import datetime as dt
import json
import os
import re
import sys

import schedule

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# (task, log path, description) - only sources that measure whole-run wall clock.
SOURCES = [
    ("RobonerRefresh", r"C:\FFL Robo Owner\refresh.log",
     "=== refresh start === / === refresh done ==="),
]

REFUSED = [
    ("GroupMeArchiveSync", r"C:\GroupMe Archive\sync.log",
     "bracketed timestamps are one reused stamp - every run measures 0s"),
    ("RobonerLineup / RobonerRoster", r"C:\FFL Robo Owner\inseason.log",
     "cascade step-sums exclude wrapper + interpreter start; 30s vs 155s measured"),
    ("NFLModelCaptureDaily", r"C:\NFL Model\capture.log",
     "no timestamps in the log at all"),
]

_TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]")


def parse_refresh(path):
    """[(started, seconds)] for every completed run in refresh.log."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError as exc:
        print(f"  cannot read {path}: {exc}")
        return []
    runs, start = [], None
    for line in lines:
        m = _TS.match(line)
        if not m:
            continue
        try:
            t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if "=== refresh start ===" in line:
            start = t                      # an unfinished run is simply replaced
        elif "=== refresh done" in line and start:
            secs = (t - start).total_seconds()
            if 0 < secs <= 6 * 3600:
                runs.append((start, secs))
            start = None
    return runs


PARSERS = {"RobonerRefresh": parse_refresh}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write job-history.json (default: dry run)")
    a = ap.parse_args()

    schedule._load_hist()
    hist = schedule._hist
    changed = []

    for task, path, how in SOURCES:
        print(f"{task}\n  source: {path}\n  markers: {how}")
        runs = PARSERS[task](path)
        if not runs:
            print("  no complete runs found\n")
            continue
        runs.sort()
        cur = hist.get(task, {}) or {}
        live = list(cur.get("samples") or [])

        # The newest run is usually in both: the poller watched it happen and the
        # log recorded it. Only the poller's most recent sample carries a
        # timestamp (last_run), so that is the one that can be matched - against
        # each backfilled run's end time. Here 06:30:02 + 5005s = 07:53:27 versus
        # a last_run of 07:53:29, which is plainly the same run counted twice.
        dropped = 0
        if live and cur.get("last_run"):
            try:
                lr = dt.datetime.fromisoformat(cur["last_run"])
                for start, secs in runs:
                    if abs(((start + dt.timedelta(seconds=secs)) - lr)
                           .total_seconds()) <= 300:
                        live = live[:-1]
                        dropped = 1
                        break
            except ValueError:
                pass
        if dropped:
            print(f"  de-duplicated 1 poller sample already present in the log")

        # Backfilled runs are older than anything the poller saw, so they go in
        # front; the window then keeps the most recent HIST_KEEP.
        merged = [int(s) for _, s in runs] + live
        merged = merged[-schedule.HIST_KEEP:]

        win = merged[-schedule.MEDIAN_WINDOW:]
        before = schedule._duration_for(task, schedule._profile(task))
        print(f"  found {len(runs)} runs, {runs[0][0].date()} to {runs[-1][0].date()}")
        print(f"  keeping {len(merged)} (cap {schedule.HIST_KEEP}), "
              f"estimate uses last {len(win)}")
        print(f"  before: {before[0]}s [{before[1]}, n={before[2]}]")
        entry = dict(cur)
        entry["samples"] = merged
        entry["last_dur"] = int(runs[-1][1])
        entry["last_run"] = runs[-1][0].isoformat(timespec="seconds")
        entry["backfill"] = {"n": len(runs), "source": os.path.basename(path),
                             "at": dt.datetime.now().isoformat(timespec="seconds")}
        hist[task] = entry
        after = schedule._duration_for(task, schedule._profile(task))
        print(f"  after:  {after[0]}s [{after[1]}, n={after[2]}, "
              f"range {after[3]}-{after[4]}s]\n")
        changed.append(task)

    print("refused, and why:")
    for task, path, why in REFUSED:
        print(f"  {task:<28} {why}")
    print()

    if not changed:
        print("nothing to write.")
        return 0
    if not a.apply:
        print(f"DRY RUN - would update {', '.join(changed)}. "
              f"Re-run with --apply to write.")
        return 0
    schedule._hist_dirty = True
    schedule._save_hist()
    print(f"wrote {schedule.HIST_FILE}: {', '.join(changed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
