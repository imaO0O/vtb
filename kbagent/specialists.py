"""Вопросы специалисту банка и пополнение базы знаний их ответами.

Помощник честно отказывается, когда ответа в законе и разъяснениях ФНС нет, — но клиент не должен упираться
в тупик. Он передаёт вопрос специалисту (журнал logs/handoff.jsonl), специалист отвечает со ссылкой на норму,
и ответ публикуется в базу знаний (data/raw/bank/answers.txt, формат разъяснений ФНС): со следующего такого
вопроса помощник отвечает сам — с пометкой «Ответ специалиста банка» и датой. Тема без ответа на странице
«Для банка» закрывается сама. Так база знаний растёт из реальных вопросов клиентов.
"""
import json
import time
import uuid
from pathlib import Path

from .config import settings
from .ingest import load_catalog
from .privacy import mask

ANSWERS_FILE = "bank/answers.txt"
HEADER = ("# Ответы специалистов банка на вопросы клиентов, которых не было в законе и разъяснениях ФНС\n"
          "# Пополняется со страницы «Для банка»; каждый ответ — со ссылкой на норму и датой публикации\n\n")


def _queue() -> Path:
    return settings.log_dir / "handoff.jsonl"


def _read() -> list[dict]:
    path = _queue()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(items: list[dict]) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    _queue().write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")


def ask_specialist(question: str, client: str | None = None, answer: str = "") -> dict:
    """Клиент передал вопрос специалисту. Персональные данные в вопросе маскируются."""
    items = _read()
    item = {"id": uuid.uuid4().hex[:10], "time": time.strftime("%Y-%m-%d %H:%M:%S"), "client": client,
            "question": mask(question), "assistant": mask(answer)[:500], "status": "open"}
    items.append(item)
    _write(items)
    return item


def queue(status: str | None = None) -> list[dict]:
    return [i for i in _read() if status is None or i.get("status") == status]


def answers_path() -> Path:
    return settings.raw_dir / ANSWERS_FILE


def publish_answer(item_id: str, question: str, answer: str, basis: str, author: str = "специалист банка",
                   index=None) -> int:
    """Ответ специалиста — в базу знаний. index (store.Index) — сразу добавить в поиск без пересборки всего
    индекса; без него ответ подхватит следующая пересборка. Возвращает число новых фрагментов."""
    question, answer, basis = question.strip(), answer.strip(), basis.strip()
    if not question or not answer or not basis:
        raise ValueError("Нужны вопрос, ответ и норма-основание")
    path = answers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else HEADER
    text += (f"Вопрос: {question}\n{answer}\nИсточник: {basis} (ответ дан {time.strftime('%d.%m.%Y')}, "
             f"{author})\n\n")
    path.write_text(text, encoding="utf-8")
    items = _read()
    for i in items:
        if i["id"] == item_id:
            i.update(status="answered", answered=time.strftime("%Y-%m-%d %H:%M:%S"), answer=answer, basis=basis)
    _write(items)
    if index is None:
        return 0
    return len(index.add_document(path, load_catalog(settings.raw_dir), settings.index_dir))


def published() -> list[dict]:
    """Опубликованные ответы специалистов: вопрос, ответ, основание."""
    path = answers_path()
    if not path.exists():
        return []
    out = []
    for block in path.read_text(encoding="utf-8").split("\nВопрос: ")[1:]:
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        basis = next((ln[len("Источник: "):] for ln in lines if ln.startswith("Источник: ")), "")
        out.append({"question": lines[0], "answer": "\n".join(ln for ln in lines[1:] if not ln.startswith("Источник: ")),
                    "basis": basis})
    return out
