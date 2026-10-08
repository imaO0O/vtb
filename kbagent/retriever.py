"""Гибридный поиск: по словам (BM25) + по смыслу (bge-m3) → объединение → переранжирование.

Поиск по словам находит точные термины, номера статей и суммы;
поиск по смыслу — перефразированные вопросы. Списки объединяются методом
Reciprocal Rank Fusion, затем переранжировщик bge-reranker-v2-m3 оценивает
вероятность того, что фрагмент отвечает на вопрос. Эта оценка — первый
фильтр от выдумок: если лучший фрагмент ниже порога, модель не вызывается.
"""
from dataclasses import dataclass

from . import llm
from .config import settings
from .ingest import Chunk
from .store import Index

RRF_K = 60
RERANK_POOL = 30          # кандидатов на переранжирование: при 20 терялись нужные пункты («кто выдаёт наряд» — п. 5.3)


@dataclass
class Hit:
    chunk: Chunk
    score: float              # вероятность релевантности (переранжировщик) или косинус
    rank_words: int | None    # место в поиске по словам
    rank_meaning: int | None  # место в поиске по смыслу
    neighbor: bool = False    # добавлен как соседний фрагмент найденного


@dataclass
class Retrieval:
    hits: list[Hit]           # лучшие фрагменты для контекста модели
    best_score: float
    confident: bool
    scored_by: str            # "reranker" или "embeddings"


def retrieve(index: Index, question: str, allowed: set[str], docs: set[str] | None = None) -> Retrieval:
    """docs — искать только в этих документах (вопрос называет документ: «по 422-ФЗ», «по НК»)."""
    words = index.search_words(question, allowed, settings.candidates, docs)
    qvec = llm.embed([question])[0]
    meaning = index.search_meaning(qvec, allowed, settings.candidates, docs)

    fused: dict[int, float] = {}
    rank_w = {i: r for r, (i, _) in enumerate(words)}
    rank_m = {i: r for r, (i, _) in enumerate(meaning)}
    for ranks in (rank_w, rank_m):
        for i, r in ranks.items():
            fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + r + 1)
    pool = sorted(fused, key=fused.get, reverse=True)[:RERANK_POOL]
    cosine = dict(meaning)

    scored_by = "embeddings"
    scores = {i: cosine.get(i, float(index.vectors[i] @ qvec)) for i in pool}
    threshold = settings.dense_threshold
    if settings.use_reranker and pool:
        try:
            probs = llm.rerank(question, [index.chunks[i].search_text for i in pool])
            scores = dict(zip(pool, probs))
            scored_by, threshold = "reranker", settings.rerank_threshold
        except llm.ModelError:
            pass  # переранжировщик недоступен — работаем на косинусной близости

    ranked = sorted(pool, key=scores.get, reverse=True)[:settings.context_chunks]
    hits = [Hit(index.chunks[i], scores[i], rank_w.get(i), rank_m.get(i)) for i in ranked]
    hits += _neighbors(index, ranked)
    best = hits[0].score if hits else 0.0
    return Retrieval(hits=hits, best_score=best, confident=best >= threshold, scored_by=scored_by)


def _neighbors(index: Index, ranked: list[int]) -> list[Hit]:
    """Соседние фрагменты лучших находок: ответ часто продолжается в следующем пункте
    («искрение — отказ категории А» в п. 2.2, а «действия при отказе категории А» — в п. 3.1)."""
    if not ranked:
        return []
    taken, extra = set(ranked), []
    top = ranked[0]
    wanted = [(top, +1), (top, +2), (top, -1)] + [(i, +1) for i in ranked[1:3]]
    for i, step in wanted:
        j = i + step
        if 0 <= j < len(index.chunks) and j not in taken and index.chunks[j].doc_id == index.chunks[i].doc_id:
            taken.add(j)
            extra.append(Hit(index.chunks[j], 0.0, None, None, neighbor=True))
    return extra
