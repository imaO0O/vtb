"""Строит индекс по всем документам из data/raw (нужен запущенный сервер bge-m3).

    python scripts/build_index.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kbagent.config import settings  # noqa: E402
from kbagent.store import Index  # noqa: E402

if __name__ == "__main__":
    Index.build(settings.raw_dir, settings.index_dir)
    print(f"Индекс сохранён в {settings.index_dir}")
