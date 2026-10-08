# Запускает три сервера llama.cpp в фоне: языковая модель (8080),
# векторы bge-m3 (8081), переранжировщик bge-reranker-v2-m3 (8082).
# Логи пишутся в папку logs. Остановка: scripts\stop_servers.bat
#   scripts\start_servers.bat              — с видеокартой (4 параллельных слота)
#   scripts\start_servers.bat -Cpu         — только процессор (ноутбук): профиль, подобранный замерами
#   scripts\start_servers.bat -Cpu -Threads 8   — число потоков вручную (по умолчанию — все логические)
#   scripts\start_servers.bat -SmallGpu    — ноутбук с видеокартой NVIDIA на 4–8 ГБ: «эксперты» модели
#                                            в оперативной памяти, остальное на видеокарте
param([switch]$Cpu, [switch]$SmallGpu, [int]$Threads = 0)

$root = (Resolve-Path "$PSScriptRoot\..").Path
$bin = Join-Path $root "tools\llama.cpp\llama-server.exe"
$models = Join-Path $root "models"
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logs | Out-Null

if (-not (Test-Path $bin)) {
    Write-Host "Не найден $bin. Сначала выполните: python scripts\setup.py" -ForegroundColor Red
    exit 1
}

$llm = @("-m", "$models\GigaChat3.1-10B-A1.8B-q4_K_M.gguf", "--jinja")
$embed = @("-m", "$models\bge-m3-Q8_0.gguf", "--embedding", "--pooling", "cls")
$rerank = @("-m", "$models\bge-reranker-v2-m3-Q8_0.gguf", "--reranking")
if ($Cpu) {
    # Профиль процессора (замеры — eval/results/laptop.md): у языковой модели все логические потоки,
    # кэш внимания f16 (точнее q8_0 и на процессоре быстрее), крупный пакет и один слот — подсказка
    # обрабатывается в 2,3 раза быстрее, текст печатается в 1,8 раза быстрее. Поисковые модели — с 4 слотами:
    # 20 фрагментов оцениваются параллельно, в 3 раза быстрее, чем по одному.
    $t = if ($Threads -gt 0) { $Threads } else { [Environment]::ProcessorCount }
    $llm += @("-ngl", "0", "-t", "$t", "-fa", "on", "-ctk", "f16", "-ctv", "f16", "-c", "16384", "-np", "1",
              "-b", "4096", "-ub", "2048")
    $embed += @("-ngl", "0", "-c", "16384", "-b", "8192", "-ub", "8192", "-np", "4")
    $rerank += @("-ngl", "0", "-c", "16384", "-b", "8192", "-ub", "8192", "-np", "4")
} elseif ($SmallGpu) {
    # Модель смешанная (MoE): 10 млрд параметров, из них на каждый токен работают 1,8 млрд. Веса «экспертов»
    # (основной объём) остаются в оперативной памяти, внимание и общие слои — на видеокарте.
    $t = if ($Threads -gt 0) { $Threads } else { [Environment]::ProcessorCount }
    $llm += @("-ngl", "999", "--n-cpu-moe", "99", "-t", "$t", "-fa", "on", "-ctk", "f16", "-ctv", "f16",
              "-c", "16384", "-np", "1", "-b", "4096", "-ub", "2048")
    $embed += @("-ngl", "999", "-c", "16384", "-b", "8192", "-ub", "8192", "-np", "4")
    $rerank += @("-ngl", "999", "-c", "16384", "-b", "8192", "-ub", "8192", "-np", "4")
} else {
    $llm += @("-ngl", "999", "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "-c", "32768", "-np", "4")
    $embed += @("-ngl", "999", "-c", "16384", "-b", "8192", "-ub", "8192", "-np", "4")
    $rerank += @("-ngl", "999", "-c", "16384", "-b", "8192", "-ub", "8192", "-np", "4")
}
$servers = @(
    @{ Name = "llm"; Port = 8080; Args = $llm },
    @{ Name = "embed"; Port = 8081; Args = $embed },
    @{ Name = "rerank"; Port = 8082; Args = $rerank }
)

function Test-Health($port) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/health" -TimeoutSec 2
        return $r.StatusCode -eq 200
    } catch { return $false }
}

foreach ($s in $servers) {
    if (Test-Health $s.Port) { Write-Host "$($s.Name): уже работает на порту $($s.Port)"; continue }
    $argList = ($s.Args + @("--host", "127.0.0.1", "--port", "$($s.Port)")) |
        ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
    Start-Process -FilePath $bin -ArgumentList $argList -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "$($s.Name).out.log") `
        -RedirectStandardError (Join-Path $logs "$($s.Name).log")
    Write-Host "$($s.Name): запускается на порту $($s.Port)…"
}

$deadline = (Get-Date).AddMinutes(3)
foreach ($s in $servers) {
    while (-not (Test-Health $s.Port)) {
        if ((Get-Date) -gt $deadline) {
            Write-Host "$($s.Name): не запустился, смотрите logs\$($s.Name).log" -ForegroundColor Red
            exit 1
        }
        Start-Sleep -Seconds 1
    }
    Write-Host "$($s.Name): готов" -ForegroundColor Green
}
Write-Host "Все модели запущены. Чат: scripts\run_chat.bat"
