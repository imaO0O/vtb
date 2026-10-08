"""Клиент OpenAI-совместимых серверов: llama.cpp llama-server или Ollama.

Используются три модели:
  * языковая модель (GigaChat 3.1 Lightning) — ответы, выбор источника, SQL;
  * bge-m3 — векторы для поиска по смыслу;
  * bge-reranker-v2-m3 — оценка, отвечает ли фрагмент на вопрос.
"""
import math
import time

import numpy as np
import requests

from .config import settings


class ModelError(RuntimeError):
    pass


def _post(url: str, payload: dict) -> dict:
    try:
        r = requests.post(url, json=payload, timeout=settings.request_timeout)
    except requests.RequestException as e:
        raise ModelError(f"Сервер модели недоступен: {url} ({e.__class__.__name__})") from e
    if r.status_code != 200:
        raise ModelError(f"{url}: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def chat(messages: list[dict], max_tokens: int | None = None, temperature: float = 0.0) -> tuple[str, dict]:
    """Возвращает текст ответа и статистику (токены, скорость, время)."""
    t0 = time.time()
    data = _post(f"{settings.llm_url}/chat/completions", {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or settings.max_answer_tokens,
        # повторное использование кэша подсказок в llama.cpp меняет вычисления на последних знаках,
        # и при «жадной» генерации ответ на тот же вопрос может отличаться; для воспроизводимости — без кэша
        "cache_prompt": not settings.deterministic,
    })
    text = data["choices"][0]["message"].get("content") or ""
    timings = data.get("timings", {})
    stats = {
        "seconds": round(time.time() - t0, 2),
        "prompt_tokens": data.get("usage", {}).get("prompt_tokens"),
        "completion_tokens": data.get("usage", {}).get("completion_tokens"),
        "tokens_per_second": round(timings.get("predicted_per_second", 0), 1) or None,
    }
    return text.strip(), stats


def embed(texts: list[str], batch: int = 32, progress=None) -> np.ndarray:
    """Нормированные векторы bge-m3. progress(готово, всего) вызывается после каждой пачки."""
    out = []
    for i in range(0, len(texts), batch):
        data = _post(f"{settings.embed_url}/embeddings", {"input": texts[i:i + batch], "model": "bge-m3"})
        out.extend(d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"]))
        if progress:
            progress(len(out), len(texts))
    vectors = np.asarray(out, dtype=np.float32)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def rerank(query: str, documents: list[str]) -> list[float]:
    """Вероятность (0–1), что фрагмент отвечает на вопрос."""
    data = _post(f"{settings.rerank_url}/rerank", {"query": query, "documents": documents, "model": "bge-reranker"})
    scores = [0.0] * len(documents)
    for item in data["results"]:
        scores[item["index"]] = 1 / (1 + math.exp(-item["relevance_score"]))
    return scores


def health() -> dict[str, bool]:
    status = {}
    for name, url in (("llm", settings.llm_url), ("embed", settings.embed_url), ("rerank", settings.rerank_url)):
        try:
            r = requests.get(url.rsplit("/v1", 1)[0] + "/health", timeout=3)
            status[name] = r.status_code == 200
        except requests.RequestException:
            status[name] = False
    return status
