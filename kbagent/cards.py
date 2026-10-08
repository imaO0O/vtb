"""Карточки документов: название, вид, редакция, статьи или вопросы — дословно из текста.

Нужны для вопросов о самой базе знаний («какие документы у тебя есть?», «о чём закон 422-ФЗ?»):
обычный поиск находит отдельные части статей, а не документ целиком, и модель пересказывала бы
случайные фрагменты. Карточка собирается из фрагментов индекса без языковой модели, поэтому в ней
нет ничего придуманного.
"""
import re
from collections import OrderedDict

from .ingest import Chunk, load_catalog
from .config import settings
from .store import Index

SCOPE_RE = re.compile(r"област\w* применени|назначени|общие положени|введени|общие сведени|описание", re.I)


def build_cards(index: Index) -> dict[str, dict]:
    docs: "OrderedDict[str, list[Chunk]]" = OrderedDict()
    for c in index.chunks:
        docs.setdefault(c.doc_id, []).append(c)
    catalog = load_catalog(settings.raw_dir)
    cards = {}
    for doc_id, chunks in docs.items():
        first = chunks[0]
        info = catalog.get(doc_id, {})
        if first.article:
            # закон: перечень статей «Статья 4. Налогоплательщики…»
            sections = list(dict.fromkeys(f"Статья {c.article}. {c.heading}" for c in chunks if c.article))
            scope = " ".join(c.text for c in chunks if c.article == first.article)
        elif first.doc_kind.startswith("Разъяснения"):
            sections = list(dict.fromkeys(re.sub(r"\s*\(продолжение\)$", "", c.heading or "") for c in chunks))
            scope = (f"Ответы ФНС России на частые вопросы о налоге на профессиональный доход: {len(sections)} "
                     "вопросов — кто может стать самозанятым, регистрация, чеки, налог и вычет, ограничения режима.")
        else:
            sections = list(dict.fromkeys(
                h for h in (c.heading for c in chunks)
                if h and len(h) < 90 and re.match(r"^\d{1,2}\.?\s+[А-ЯЁA-Z][а-яёa-z]", h)))
            scope_chunk = next((c for c in chunks if c.heading and SCOPE_RE.search(c.heading)), None) or first
            scope = scope_chunk.text
        pages = max((c.page_end or 0) for c in chunks)
        cards[doc_id] = {
            "doc_id": doc_id, "title": first.doc_title, "short": first.short or first.doc_title,
            "kind": first.doc_kind, "access": first.access, "pages": pages or None, "chunks": len(chunks),
            "sections": sections, "scope": re.sub(r"\s+", " ", scope)[:700], "edition": info.get("edition"),
            "url": info.get("url"),
        }
    return cards


def card_text(card: dict, sections: int = 40) -> str:
    parts = [f"Документ: {card['title']}", f"Вид: {card['kind']}"]
    if card.get("edition"):
        parts.append(f"Редакция: {card['edition']}")
    if card.get("pages"):
        parts.append(f"Объём: {card['pages']} стр.")
    parts.append(f"Начало документа: {card['scope']}")
    if card["sections"]:
        shown = card["sections"][:sections]
        more = len(card["sections"]) - len(shown)
        parts.append("Содержание: " + "; ".join(shown) + (f"; и ещё {more}" if more > 0 else ""))
    return "\n".join(parts)


def card_chunk(card: dict) -> Chunk:
    """Карточка в виде фрагмента — чтобы ответ по ней проходил те же проверки ссылок."""
    return Chunk(id=f"{card['doc_id']}#card", doc_id=card["doc_id"], doc_title=card["title"],
                 doc_kind=card["kind"], access=card["access"], clause=None, heading="Карточка документа",
                 page_start=None, page_end=None, text=card_text(card), short=card.get("short"), url=card.get("url"))
