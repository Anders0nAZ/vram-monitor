# Opens the VRAM Monitor in a tight, chrome-less Chrome app window and pins it
# always-on-top (topmost while visible; minimizing hides it, restoring keeps it on top).
$url  = 'http://localhost:11435'
$size = '720,440'
$pos  = '120,80'

$chrome = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

Start-Sleep -Seconds 2   # give the server a moment to come up

if (-not $chrome) { Start-Process $url; return }

Start-Process $chrome -ArgumentList @(
  "--app=$url",
  "--window-size=$size",
  "--window-position=$pos",
  "--user-data-dir=$env:LocalAppData\VRAMMonitor\chrome"
)

# --- pin always-on-top ---
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinTop {
  [DllImport("user32.dll")]
  public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter,
    int X, int Y, int cx, int cy, uint uFlags);
}
"@
$HWND_TOPMOST = [IntPtr](-1)
$FLAGS = 0x0001 -bor 0x0002 -bor 0x0010   # NOSIZE | NOMOVE | NOACTIVATE

for ($i = 0; $i -lt 40; $i++) {           # wait up to ~10s for the window to appear
  $p = Get-Process chrome -ErrorAction SilentlyContinue |
       Where-Object { $_.MainWindowTitle -eq 'VRAM Monitor' } | Select-Object -First 1
  if ($p) {
    [WinTop]::SetWindowPos($p.MainWindowHandle, $HWND_TOPMOST, 0,0,0,0, $FLAGS) | Out-Null
    break
  }
  Start-Sleep -Milliseconds 250
}
