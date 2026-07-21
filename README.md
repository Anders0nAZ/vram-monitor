# VRAM Monitor

A small local web dashboard that shows what is actually occupying your GPU's
memory, and automatically reclaims it from whichever process has gone idle.

Built for a single-GPU machine running more than one AI workload — an LLM server
and an image generation server competing for the same 24 GB — where the practical
question is never "how much VRAM is used" but "which of these is holding it, and
can I get it back without restarting anything."

Stdlib only. No pip install, no framework, no config file.

## What it does

- Polls Ollama's `/api/ps` for precise per-model VRAM usage and GPU/CPU split.
- Polls `nvidia-smi` for total board usage and which processes are attached.
- Serves a phone-friendly dashboard with a live event log of loads and unloads.
- Runs a watchdog that frees ComfyUI's VRAM after a configurable idle period,
  so an image model left loaded hours ago stops crowding out the LLM.
- Exposes manual controls — free ComfyUI now, stop LLMs, toggle auto-unload,
  quit — so it can run hidden at login with no console window.

## The interesting part: you cannot measure this directly on Windows

On consumer GPUs under Windows' WDDM driver model, **`nvidia-smi` cannot report
per-process VRAM.** It will tell you the board is holding 21 GB and list the
processes attached, but not how much each one holds. That is precisely the number
you need to decide what to reclaim.

Ollama reports its own usage accurately through its API, so the workaround is to
treat everything else as a residual:

```
other_vram = board_used - ollama_reported
```

Loads and unloads by the non-API process are then inferred from jumps in that
residual rather than observed directly.

This works, but the signal is noisy in exactly one place: during an Ollama load or
unload, the residual moves for reasons that have nothing to do with ComfyUI, which
would otherwise produce false attributions. Two mitigations handle it:

- **Settle cycles** — after any known Ollama load or unload, attribution to
  "other" is suppressed for a few polls while the allocator settles.
- **Recency check** — a drop in the residual is only credited to ComfyUI if
  ComfyUI actually generated something recently. Otherwise it is left
  unattributed rather than guessed at.

The general principle: when a measurement is unreliable, it is better to
abstain than to attribute confidently and be wrong. A dashboard that says
"unknown" is more useful than one that says the wrong thing.

## Requirements

- Windows with an NVIDIA GPU (`nvidia-smi` on `PATH`)
- Python 3.9+
- [Ollama](https://ollama.com/) and/or [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
  running locally — the dashboard degrades gracefully if either is absent

## Usage

```sh
python vram_monitor.py
```

Then open <http://localhost:11435>. The console also prints your LAN and
Tailscale URLs if available, so the dashboard is reachable from a phone.

`Start-VRAMMonitor.bat` starts the server and opens the dashboard in a compact
always-on-top Chrome window. `Start-ComfyIdleUnload.bat` runs only the ComfyUI
watchdog, standalone.

If the dashboard is unreachable from another device, `Fix-Firewall.bat` removes
the accidental `pythonw.exe` block rule Windows creates when a firewall prompt is
dismissed, and allows inbound TCP 11435 from your LAN and Tailscale only — never
the public internet.

## Configuration

Constants at the top of `vram_monitor.py`:

| Setting | Default | Meaning |
|---|---|---|
| `PORT` | `11435` | Dashboard port |
| `HOST` | `0.0.0.0` | `127.0.0.1` restricts to this machine |
| `POLL_SECONDS` | `2` | Poll interval |
| `IDLE_MINUTES` | `5` | Idle time before ComfyUI's VRAM is freed |
| `AUTO_UNLOAD` | `True` | Watchdog on at startup |
| `OTHER_DELTA_MB` | `400` | Minimum residual change logged as an event |
| `SETTLE_CYCLES` | `4` | Polls to suppress attribution after a known load |

## Notes

`HOST = 0.0.0.0` binds all interfaces so a phone can reach it. There is no
authentication — this is intended for a trusted LAN or a Tailscale network, not
the open internet. Bind to `127.0.0.1` if you only need local access.

## License

MIT
