"""Запуск чата из корня проекта (там лежит .streamlit/config.toml с оформлением).

    python scripts/run_chat.py
    python scripts/run_chat.py --server.port 8502 --server.headless true
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    os.chdir(ROOT)
    from streamlit.web import cli

    sys.argv = ["streamlit", "run", str(ROOT / "app" / "streamlit_app.py"), *sys.argv[1:]]
    sys.exit(cli.main())
