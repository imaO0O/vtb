"""Настройки помощника. Любое значение можно переопределить переменной окружения."""
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return type(default)(value) if not isinstance(default, bool) else value.lower() in ("1", "true", "yes")


@dataclass
class Settings:
    # OpenAI-совместимые серверы (llama.cpp llama-server или Ollama) — внутри контура банка
    llm_url: str = _env("KB_LLM_URL", "http://127.0.0.1:8080/v1")
    llm_model: str = _env("KB_LLM_MODEL", "GigaChat3.1-10B-A1.8B")
    embed_url: str = _env("KB_EMBED_URL", "http://127.0.0.1:8081/v1")
    rerank_url: str = _env("KB_RERANK_URL", "http://127.0.0.1:8082/v1")
    use_reranker: bool = _env("KB_USE_RERANKER", True)

    # данные
    raw_dir: Path = ROOT / "data" / "raw"
    index_dir: Path = ROOT / "data" / "index"
    clients_path: Path = ROOT / "data" / "demo" / "clients.yaml"   # демо-клиенты банка и их поступления
    tessdata_dir: Path = ROOT / "tools" / "tessdata"               # языки распознавания сканов
    log_dir: Path = ROOT / "logs"

    # поиск
    candidates: int = _env("KB_CANDIDATES", 30)      # из каждого поиска (слова и смысл)
    context_chunks: int = _env("KB_CONTEXT_CHUNKS", 5)  # фрагментов в контекст модели
    # Порог отказа: если лучший фрагмент оценён переранжировщиком ниже порога,
    # помощник отвечает «в законе и разъяснениях ФНС этого нет», не вызывая модель.
    rerank_threshold: float = _env("KB_RERANK_THRESHOLD", 0.03)
    # Ниже этой оценки — «зона сомнения»: найденное похоже на вопрос, но может быть
    # о другом предмете. Такой ответ дополнительно проверяется моделью (answer.verify).
    confident_threshold: float = _env("KB_CONFIDENT_THRESHOLD", 0.3)
    # Если найден фрагмент сильнее этой оценки, а ответ опирается только на более слабые — ответ «не о том».
    cited_threshold: float = _env("KB_CITED_THRESHOLD", 0.1)
    # Проверять моделью каждый ответ, а не только в «зоне сомнения». Выключено: на эталоне отклоняет
    # много верных ответов (66 из 94 верно против 81 без неё) — небольшая модель плохо судит сама себя
    verify_always: bool = _env("KB_VERIFY_ALWAYS", False)
    # Запасной порог по косинусной близости, если переранжировщик выключен.
    dense_threshold: float = _env("KB_DENSE_THRESHOLD", 0.50)

    # генерация
    max_answer_tokens: int = _env("KB_MAX_TOKENS", 500)
    # одинаковый ответ на одинаковый вопрос (без кэша подсказок); на процессоре KB_DETERMINISTIC=0 быстрее
    deterministic: bool = _env("KB_DETERMINISTIC", True)
    # Персональные данные в журнале вопросов: маскировать (телефоны, почта, паспорт, СНИЛС, ИНН, ФИО)
    log_mask_pii: bool = _env("KB_LOG_MASK_PII", True)
    # Сколько дней хранить журнал вопросов (0 — без ограничения)
    log_retention_days: int = _env("KB_LOG_RETENTION_DAYS", 180)
    request_timeout: int = _env("KB_TIMEOUT", 300)

    # Кто что видит: клиент — закон и разъяснения; сотрудник банка — ещё и служебные документы
    roles: dict = field(default_factory=lambda: {
        "Клиент": {"all"},
        "Сотрудник банка": {"all", "admin"},
    })


settings = Settings()
