"""Скачивает llama.cpp и модели (около 8 ГБ) в папки tools/ и models/ проекта.

    python scripts/setup.py          # компьютер с видеокартой NVIDIA (CUDA 13)
    python scripts/setup.py --cpu    # ноутбук без видеокарты NVIDIA
    python scripts/setup.py --ocr    # только языки распознавания сканов (около 6 МБ)

Файлы качаются в несколько потоков (scripts/pget.py) с докачкой: если
загрузка прервалась, просто запустите команду ещё раз.
"""
import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
TOOLS = ROOT / "tools"
PGET = Path(__file__).with_name("pget.py")

LLAMA_BUILD = "b11371"   # llama.cpp не ниже b8495 — требование разработчиков GigaChat 3.1
LLAMA_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}"

MODEL_FILES = {
    "GigaChat3.1-10B-A1.8B-q4_K_M.gguf":
        "https://huggingface.co/ai-sage/GigaChat3.1-10B-A1.8B-GGUF/resolve/main/GigaChat3.1-10B-A1.8B-q4_K_M.gguf",
    "bge-m3-Q8_0.gguf":
        "https://huggingface.co/gpustack/bge-m3-GGUF/resolve/main/bge-m3-Q8_0.gguf",
    "bge-reranker-v2-m3-Q8_0.gguf":
        "https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q8_0.gguf",
}

# Языки распознавания сканов: движок Tesseract встроен в PyMuPDF, нужны только файлы языков
# (официальный репозиторий tesseract-ocr, лицензия Apache 2.0).
TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main"
TESSDATA_FILES = ["rus.traineddata", "eng.traineddata"]


def fetch(url: str, target: Path) -> None:
    if target.exists() and not Path(str(target) + ".parts").exists():
        print(f"есть: {target.name}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(PGET), url, str(target), "16", "16"], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpu", action="store_true", help="сборка llama.cpp для процессора")
    ap.add_argument("--ocr", action="store_true", help="скачать только языки распознавания сканов")
    args = ap.parse_args()

    for name in TESSDATA_FILES:
        fetch(f"{TESSDATA_URL}/{name}", TOOLS / "tessdata" / name)
    if args.ocr:
        return

    archives = ([f"llama-{LLAMA_BUILD}-bin-win-cpu-x64.zip"] if args.cpu else
                [f"llama-{LLAMA_BUILD}-bin-win-cuda-13.4-x64.zip", "cudart-llama-bin-win-cuda-13.4-x64.zip"])
    llama_dir = TOOLS / "llama.cpp"
    for name in archives:
        archive = TOOLS / name
        fetch(f"{LLAMA_URL}/{name}", archive)
        with zipfile.ZipFile(archive) as z:
            z.extractall(llama_dir)
        print(f"распакован: {name}")

    for name, url in MODEL_FILES.items():
        fetch(url, MODELS / name)

    print("\nГотово. Запуск серверов моделей: scripts\\start_servers.bat" + (" -Cpu" if args.cpu else ""))


if __name__ == "__main__":
    main()
