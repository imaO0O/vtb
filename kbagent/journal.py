"""Журналы помощника: вопросы и ответы (logs/audit.jsonl, пишет agent._log) и оценки ответов 👍/👎
(logs/feedback.jsonl). Персональные данные маскируются до записи (privacy.mask)."""
import json
import time

from .config import settings
from .privacy import mask


def _read(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_audit(limit: int = 20000) -> list[dict]:
    return _read(settings.log_dir / "audit.jsonl")[-limit:]


def save_feedback(record: dict) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), **record}
    for key in ("question", "answer", "comment"):
        if key in record:
            record[key] = mask(record[key])
    with open(settings.log_dir / "feedback.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_feedback() -> list[dict]:
    return _read(settings.log_dir / "feedback.jsonl")
