@echo off
rem ============================================================
rem  VRAM Monitor - firewall fix
rem  Removes the accidental pythonw.exe BLOCK rule (created when a
rem  Windows firewall popup was dismissed) and allows inbound TCP
rem  11435 from Tailscale + your LAN only (not the public internet).
rem  Self-elevates: you'll get one UAC prompt - click Yes.
rem ============================================================
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo.
echo === Removing accidental pythonw.exe BLOCK rules ===
powershell -NoProfile -Command "Get-NetFirewallRule -DisplayName 'pythonw.exe' -ErrorAction SilentlyContinue | Where-Object { $_.Action -eq 'Block' } | ForEach-Object { Remove-NetFirewallRule -Name $_.Name; 'removed: ' + $_.DisplayName }"

echo.
echo === Allowing inbound TCP 11435 (Tailscale 100.64/10 + your local LAN /24) ===
rem  The LAN subnet is derived from whichever adapter holds the default gateway,
rem  so this works on any network without editing. Tailscale's 100.64.0.0/10 is
rem  the shared CGNAT range used by every tailnet, not an address specific to you.
powershell -NoProfile -Command "$ip = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1).IPv4Address.IPAddress; if (-not $ip) { 'could not determine LAN address - skipping LAN scope'; $scope = '100.64.0.0/10' } else { $lan = (($ip -split '\.')[0..2] -join '.') + '.0/24'; \"LAN scope: $lan\"; $scope = \"100.64.0.0/10,$lan\" }; if (-not (Get-NetFirewallRule -DisplayName 'VRAM Monitor (11435)' -ErrorAction SilentlyContinue)) { New-NetFirewallRule -DisplayName 'VRAM Monitor (11435)' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 11435 -Profile Private -RemoteAddress $scope | Out-Null; 'allow rule created' } else { 'allow rule already exists' }"

echo.
echo Done. The console prints the LAN and Tailscale URLs when the server starts -
echo open whichever one applies on your phone.
pause
