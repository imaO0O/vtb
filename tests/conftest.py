"""Общие заготовки тестов. Тесты не обращаются к моделям — проверяется логика вокруг них."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent.ingest import Chunk  # noqa: E402
from kbagent.retriever import Hit  # noqa: E402


def make_chunk(text: str, doc_id: str = "fz422", article: str | None = "4", clause: str | None = "2",
               item: str | None = None, heading: str | None = "Налогоплательщики", kind: str = "Федеральный закон",
               short: str = "422-ФЗ", cid: str | None = None) -> Chunk:
    return Chunk(id=cid or f"{doc_id}#{article}-{clause}-{item}", doc_id=doc_id, doc_title=f"Документ {doc_id}",
                 doc_kind=kind, access="all", clause=clause, heading=heading, page_start=None, page_end=None,
                 text=text, article=article, item=item, short=short)


def make_hit(text: str, score: float = 0.9, **kw) -> Hit:
    return Hit(make_chunk(text, **kw), score, 0, 0)


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    """Журналы — во временной папке."""
    from kbagent.config import settings
    monkeypatch.setattr(settings, "log_dir", tmp_path / "logs")
    return settings
