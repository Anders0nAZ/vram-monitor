' Starts the Ollama server on the gated port, with no console window.
'
' NOT "ollama app.exe" (the tray app): it forces its child server onto the default
' port regardless of OLLAMA_HOST, which would steal :11434 from the VRAM admission
' gate and leave two processes bound to the same port with undefined delivery.
'
' Verified 2026-08-31 on Ollama 0.33.1 that a direct `ollama serve` still loads
' models 100% onto the GPU, so the tray app is not needed for CUDA discovery.
'
' To revert to the stock setup: re-enable Ollama.lnk in the Startup folder, delete
' this file from Startup, and clear the OLLAMA_HOST user environment variable.

Set sh = CreateObject("WScript.Shell")
sh.Environment("PROCESS")("OLLAMA_HOST") = "127.0.0.1:11436"
exe = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Ollama\ollama.exe"
sh.Run """" & exe & """ serve", 0, False
