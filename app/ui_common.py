"""Общее для страниц: оформление, подписи, кэшируемые ресурсы."""
import json
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kbagent import account, llm  # noqa: E402
from kbagent.agent import Agent  # noqa: E402
from kbagent.config import settings  # noqa: E402

APP_NAME = "Первый чек"
APP_TAGLINE = "ИИ-помощник для начинающих самозанятых"
LOGO = str(ROOT / "app" / "static" / "logo.svg")
USER_AVATAR = ":material/person:"

ROUTE_LABELS = {"documents": "Закон и разъяснения ФНС", "account": "Ваш счёт + закон", "help": "Справка"}
STATUS_LABELS = {"answer": "Ответ", "no_data": "Нет в базе знаний", "model_refused": "Нет в найденном",
                 "unverified": "Не подтверждено", "clarify": "Уточнение", "info": "Справка", "error": "Ошибка"}

# метка над ответом: (текст, цвет, значок) — статус передаётся и словом, и значком, не только цветом
BADGES = {
    "documents": ("Ответ по закону", "blue", ":material/verified:"),
    "account": ("Расчёт по вашему счёту", "violet", ":material/account_balance_wallet:"),
    "no_data": ("В законе ответа нет", "gray", ":material/search_off:"),
    "model_refused": ("В законе ответа нет", "gray", ":material/search_off:"),
    "unverified": ("Ответ не подтверждён ссылкой", "orange", ":material/warning:"),
    "clarify": ("Нужно уточнение", "orange", ":material/help:"),
    "info": ("Справка", "gray", ":material/info:"),
    "error": ("Ошибка", "red", ":material/error:"),
}

CSS = """
<style>
[data-testid="stMainBlockContainer"], [data-testid="stBottomBlockContainer"] { max-width: 920px; }
/* кнопки-источники под ответом — компактнее */
[class*="st-key-sources"] button { padding: .2rem .7rem; min-height: 0; justify-content: flex-start; text-align: left; }
[class*="st-key-sources"] button > div { justify-content: flex-start; }
[class*="st-key-sources"] button p { font-size: .84rem; }
/* подсказки по счёту */
[class*="st-key-hint-"] { border-radius: .7rem; padding: .55rem .8rem; margin-bottom: .4rem; }
[class*="st-key-hint-error"] { background: #FDECEC; border: 1px solid #F3C4C4; }
[class*="st-key-hint-warning"] { background: #FFF6E0; border: 1px solid #F1DDA6; }
[class*="st-key-hint-info"] { background: #EAF0FB; border: 1px solid #CFDAF3; }
[class*="st-key-hint-"] p { margin-bottom: .2rem; }
[data-testid="stCaptionContainer"] { color: #4A5675; }
/* телефон: без аватаров — ответу вся ширина экрана */
@media (max-width: 640px) {
  [data-testid^="stChatMessageAvatar"] { display: none; }
  [data-testid="stChatMessage"] { gap: 0; padding-left: .2rem; padding-right: .2rem; }
}
</style>
"""


def inject_css(extra: str = "") -> None:
    st.markdown(CSS + extra, unsafe_allow_html=True)


@st.cache_resource(show_spinner="Загружаю базу знаний…")
def get_agent() -> Agent:
    return Agent()


@st.cache_data(ttl=3600, show_spinner=False)
def clients(day: str) -> dict:
    """Демо-клиенты; day — ключ кэша, чтобы даты операций обновлялись раз в день."""
    return account.load_clients()


@st.cache_data(ttl=15, show_spinner=False)
def health() -> dict[str, bool]:
    return llm.health()


def index_meta() -> dict:
    path = settings.index_dir / "meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
