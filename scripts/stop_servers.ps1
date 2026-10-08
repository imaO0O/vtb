# Останавливает серверы моделей llama.cpp, запущенные start_servers.
Get-Process llama-server -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Host "Серверы моделей остановлены"
