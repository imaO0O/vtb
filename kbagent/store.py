"""Индекс фрагментов: полнотекстовый (BM25) и векторный (bge-m3).

Векторы хранятся в numpy и ищутся точным перебором: для базы знаний
в тысячи и десятки тысяч фрагментов это занимает миллисекунды, не требует
отдельного сервера и не блокирует файлы при одновременной работе чата и
скрипта оценки. При росте до сотен тысяч фрагментов этот класс заменяется
на векторную СУБД (например, Qdrant) без изменения остального кода.
"""
import json
import math
import time
from collections import Counter
from pathlib import Path

import numpy as np

from . import llm
from .ingest import Chunk, parse_corpus, parse_document
from .text import tokenize


class BM25:
    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(t) for t in docs_tokens]
        self.len = np.array([len(t) for t in docs_tokens], dtype=np.float32)
        self.avg = float(self.len.mean()) if len(self.len) else 0.0
        df = Counter(term for t in docs_tokens for term in set(t))
        n = len(docs_tokens)
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}
        self.postings: dict[str, list[int]] = {}
        for i, counts in enumerate(self.tf):
            for term in counts:
                self.postings.setdefault(term, []).append(i)

    def scores(self, query_tokens: list[str]) -> np.ndarray:
        s = np.zeros(len(self.tf), dtype=np.float32)
        for term in set(query_tokens):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i in self.postings[term]:
                f = self.tf[i][term]
                s[i] += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
        return s


class Index:
    def __init__(self, chunks: list[Chunk], vectors: np.ndarray):
        self.chunks = chunks
        self.vectors = vectors
        self.bm25 = BM25([tokenize(c.search_text) for c in chunks])
        self.access = np.array([c.access for c in chunks])

    # ---------- построение и загрузка ----------

    @classmethod
    def build(cls, raw_dir: Path, index_dir: Path, log=print) -> "Index":
        t0 = time.time()
        chunks = parse_corpus(raw_dir)
        log(f"Фрагментов: {len(chunks)} из {len({c.doc_id for c in chunks})} документов")
        vectors = llm.embed([c.search_text for c in chunks])
        log(f"Векторы: {vectors.shape} за {time.time() - t0:.0f} с")
        index = cls(chunks, vectors)
        index.save(index_dir)
        return index

    def save(self, index_dir: Path) -> None:
        index_dir.mkdir(parents=True, exist_ok=True)
        with open(index_dir / "chunks.jsonl", "w", encoding="utf-8") as f:
            for c in self.chunks:
                f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
        np.save(index_dir / "vectors.npy", self.vectors)
        meta = {"chunks": len(self.chunks), "documents": len({c.doc_id for c in self.chunks}),
                "built": time.strftime("%Y-%m-%d %H:%M"), "embedding_model": "bge-m3"}
        (index_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _rebuild_word_index(self) -> None:
        self.bm25 = BM25([tokenize(c.search_text) for c in self.chunks])
        self.access = np.array([c.access for c in self.chunks])

    def add_document(self, path: Path, catalog: dict, index_dir: Path, progress=None,
                     stats: dict | None = None) -> list[Chunk]:
        """Добавляет (или заменяет) один документ без пересборки всего индекса.
        progress(готово, всего) — для полосы прогресса; stats — счётчики распознанных сканов."""
        new = parse_document(path, catalog, stats)
        self.remove_document(path.stem, index_dir, save=False)
        if new:
            vectors = llm.embed([c.search_text for c in new], progress=progress)
            self.chunks = self.chunks + new
            self.vectors = np.vstack([self.vectors, vectors]) if len(self.vectors) else vectors
            self._rebuild_word_index()
        self.save(index_dir)
        return new

    def update_meta(self, doc_id: str, index_dir: Path, **fields) -> int:
        """Меняет сведения о документе во всех его фрагментах без пересчёта векторов:
        access (отменённая редакция → archive), doc_kind (утверждён), doc_title."""
        changed = 0
        for c in self.chunks:
            if c.doc_id == doc_id and any(getattr(c, k) != v for k, v in fields.items()):
                for k, v in fields.items():
                    setattr(c, k, v)
                changed += 1
        if changed:
            self.access = np.array([c.access for c in self.chunks])
            self.save(index_dir)
        return changed

    def doc_ids(self) -> set[str]:
        return {c.doc_id for c in self.chunks}

    def remove_document(self, doc_id: str, index_dir: Path, save: bool = True) -> int:
        keep = [i for i, c in enumerate(self.chunks) if c.doc_id != doc_id]
        removed = len(self.chunks) - len(keep)
        if removed:
            self.chunks = [self.chunks[i] for i in keep]
            self.vectors = self.vectors[keep]
            self._rebuild_word_index()
            if save:
                self.save(index_dir)
        return removed

    @classmethod
    def load(cls, index_dir: Path) -> "Index":
        with open(index_dir / "chunks.jsonl", encoding="utf-8") as f:
            chunks = [Chunk(**json.loads(line)) for line in f]
        return cls(chunks, np.load(index_dir / "vectors.npy"))

    # ---------- поиск ----------

    def _mask(self, allowed: set[str], docs: set[str] | None = None) -> np.ndarray:
        mask = np.isin(self.access, list(allowed))
        if docs:                                   # вопрос называет документ — ищем только в нём
            mask &= np.isin(np.array([c.doc_id for c in self.chunks]), list(docs))
        return mask

    def search_words(self, query: str, allowed: set[str], k: int, docs: set[str] | None = None) -> list[tuple[int, float]]:
        s = self.bm25.scores(tokenize(query))
        s[~self._mask(allowed, docs)] = 0
        top = np.argsort(-s)[:k]
        return [(int(i), float(s[i])) for i in top if s[i] > 0]

    def search_meaning(self, query_vector: np.ndarray, allowed: set[str], k: int,
                       docs: set[str] | None = None) -> list[tuple[int, float]]:
        s = self.vectors @ query_vector
        s[~self._mask(allowed, docs)] = -1
        top = np.argsort(-s)[:k]
        return [(int(i), float(s[i])) for i in top]
