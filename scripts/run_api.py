"""Запуск REST API агента (документация по адресу /docs).

    python scripts/run_api.py                    # 127.0.0.1:8000
    python scripts/run_api.py --host 0.0.0.0     # доступ из сети предприятия
"""
import argparse
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    uvicorn.run("api.server:app", host=args.host, port=args.port, app_dir=str(ROOT))
