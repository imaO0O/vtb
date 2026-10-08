"""Страница помощника: подсказки по счёту клиента, диалог, ответы со ссылками, окно источника, оценки."""
import re
import time

import pandas as pd
import streamlit as st

from app.ui_common import (APP_NAME, APP_TAGLINE, BADGES, LOGO, ROUTE_LABELS, USER_AVATAR, clients, get_agent,
                           health)
from kbagent import account
from kbagent.journal import save_feedback
from kbagent.text import law_text

EXAMPLES = [
    "Какой лимит дохода у самозанятого?",
    "Можно ли работать как самозанятый на бывшего работодателя?",
    "Что будет, если не сформировать чек?",
    "Может ли самозанятый получать больничный?",
]
ACCOUNT_EXAMPLES = ["Сколько мне осталось до лимита?", "Что мне выгоднее: ИП или самозанятость?",
                    "По каким поступлениям у меня нет чека?", "Сколько налога мне платить?"]
LEVEL_ICONS = {"error": ":material/error:", "warning": ":material/warning:", "info": ":material/info:"}


# ---------- клиент и диалог ----------

def _client() -> "account.Client | None":
    cid = st.session_state.get("client_id")
    return clients(account.today().isoformat()).get(cid) if cid else None


def _items() -> list[dict]:
    """Диалог — свой у каждого демо-клиента, чтобы при переключении не смешивались ответы о разных счетах."""
    convs = st.session_state.setdefault("convs", {})
    return convs.setdefault(st.session_state.get("client_id") or "_none", [])


def sidebar() -> None:
    with st.sidebar:
        if st.button("Новый диалог", icon=":material/add_comment:", type="primary", use_container_width=True):
            _items().clear()
            st.rerun()
        st.toggle("Показывать найденные фрагменты", key="debug",
                  help="Для разбора: фрагменты, которые нашёл поиск, но не процитировала модель, и их оценки")


# ---------- ответ ----------

def _short(label: str, limit: int = 100) -> str:
    return label if len(label) <= limit else label[:limit - 1].rstrip() + "…"


@st.dialog("Источник", width="large")
def show_source(s: dict) -> None:
    st.markdown(f"**{s['label']}**")
    st.markdown(law_text(s["text"]))
    for b in s.get("basis") or []:
        with st.expander(f"Основание по закону: {b['label']}", icon=":material/gavel:"):
            st.markdown(law_text(b["text"]))
            if b.get("url"):
                st.link_button("Открыть статью", b["url"], icon=":material/open_in_new:")
    if s.get("url"):
        st.link_button("Открыть оригинал", s["url"], icon=":material/open_in_new:",
                       help="Текст закона в действующей редакции или страница разъяснений ФНС")


def _on_feedback(i: int, key: str) -> None:
    item = _items()[i]
    item["feedback"] = st.session_state.get(key)
    if item["feedback"] is not None:
        save_feedback({"client": st.session_state.get("client_id"), "question": item["question"],
                       "answer": item["text"], "status": item["status"], "route": item["route"],
                       "score": "👍" if item["feedback"] == 1 else "👎"})


def _on_comment(i: int, key: str) -> None:
    item = _items()[i]
    text = (st.session_state.get(key) or "").strip()
    if text:
        item["comment"] = text
        save_feedback({"client": st.session_state.get("client_id"), "question": item["question"],
                       "answer": item["text"], "status": item["status"], "route": item["route"], "score": "👎",
                       "comment": text})


def render_answer(item: dict, i: int) -> None:
    uid = f"{st.session_state.get('client_id')}-{i}"
    kind = item["route"] if item["status"] == "answer" else item["status"]
    label, color, icon = BADGES.get(kind, BADGES["error"])
    st.badge(label, icon=icon, color=color)
    if item.get("asked"):
        st.caption(f"Понял вопрос как: «{item['asked']}»")
    st.markdown(re.sub(r"(\[\d+(?:\s*[,–-]\s*\d+)*\])", r"**\1**", item["text"]))

    # варианты уточнения и примеры нажимаются один раз
    if item.get("options") and not item.get("options_used"):
        for n, option in enumerate(item["options"]):
            if st.button(option, key=f"opt-{uid}-{n}", icon=":material/arrow_forward:"):
                item["options_used"] = True
                st.session_state.pending = option
                st.rerun()

    if item.get("rows"):
        st.dataframe(pd.DataFrame(item["rows"], columns=item["columns"]), hide_index=True, use_container_width=True)

    cited = [s for s in item.get("sources", []) if s["cited"]]
    if cited:
        with st.container(key=f"sources-{uid}"):
            st.caption("Источники — нажмите, чтобы прочитать текст закона или разъяснения ФНС")
            for s in cited:
                basis = f" → {s['basis'][0]['label']}" if s.get("basis") else ""
                if st.button(f"[{s['n']}] {_short(s['label'])}{basis}", key=f"src-{uid}-{s['n']}",
                             icon=":material/gavel:" if s.get("article") else ":material/description:",
                             use_container_width=True):
                    show_source(s)
    other = [s for s in item.get("sources", []) if not s["cited"]]
    if st.session_state.get("debug") and other:
        with st.expander("Другие найденные фрагменты"):
            for s in other:
                st.caption(f"[{s['n']}] {s['label']} — релевантность {s['score']}")

    left, right = st.columns([5, 2], vertical_alignment="center")
    seconds = f"{item['seconds']:.1f}".replace(".", ",")
    left.caption(ROUTE_LABELS.get(item["route"], "") + ("" if item["status"] == "info" else f" · {seconds} с"))
    with right:
        if item["status"] not in ("error", "info"):
            if item.get("feedback") is None:
                st.feedback("thumbs", key=f"fb-{uid}", on_change=_on_feedback, args=(i, f"fb-{uid}"))
            else:
                st.caption("Оценка: " + ("👍" if item["feedback"] == 1 else "👎"))
    if item.get("feedback") == 0 and not item.get("comment"):
        st.text_input("Что не так с ответом? (необязательно)", key=f"cm-{uid}", on_change=_on_comment,
                      args=(i, f"cm-{uid}"))


# ---------- главный экран ----------

def _hints(client: "account.Client") -> None:
    """Что банк видит по счёту: подсказки с кнопкой «подробнее» — тот же вопрос в чат."""
    items = account.warnings(client)
    if not items:
        st.success("По вашим поступлениям всё в порядке: чеки сформированы, до лимита далеко.",
                   icon=":material/check_circle:")
        return
    st.markdown("**Банк заметил по вашим поступлениям:**")
    for k, w in enumerate(items):
        with st.container(key=f"hint-{w['level']}-{k}"):
            st.markdown(f"{LEVEL_ICONS[w['level']]} **{w['title']}**  \n{w['text']}")
            if st.button("Подробнее", key=f"hint-ask-{k}", type="tertiary", icon=":material/arrow_forward:"):
                st.session_state.pending = w["ask"]
                st.rerun()


PATH_CSS = """
<style>
.path { display: flex; gap: .35rem; margin: .4rem 0 .6rem; flex-wrap: wrap; }
.path div { flex: 1 1 0; min-width: 7.5rem; padding: .45rem .6rem; border-radius: .6rem; font-size: .85rem;
            background: #F2F5FC; color: #4A5675; border: 1px solid #DCE3F2; }
.path div.done { background: #E3F4EA; color: #1E6B3C; border-color: #BFE3CC; }
.path div.now { background: #2350C8; color: #FFFFFF; border-color: #2350C8; font-weight: 600; }
.path div b { display: block; font-size: .72rem; opacity: .8; font-weight: 500; }
</style>
"""


def _path(client: "account.Client") -> None:
    """Путь клиента в банке: где он сейчас и какой следующий шаг."""
    now = account.stage(client)
    cells = []
    for k, name in enumerate(account.STAGES):
        cls = "done" if k < now else "now" if k == now else ""
        mark = "✓ пройдено" if k < now else "вы здесь" if k == now else f"шаг {k + 1}"
        cells.append(f'<div class="{cls}"><b>{mark}</b>{name}</div>')
    st.markdown(PATH_CSS + '<div class="path">' + "".join(cells) + "</div>", unsafe_allow_html=True)
    title, question = account.NEXT_STEP[now]
    if st.button(f"Следующий шаг: {title}", key="next-step", icon=":material/trending_flat:", type="primary"):
        st.session_state.pending = question
        st.rerun()


def _hero(client: "account.Client | None") -> None:
    st.image(LOGO, width=52)
    st.markdown(f"### {APP_NAME}\n{APP_TAGLINE}")
    st.markdown("Объясняю простыми словами налог для самозанятых. К каждому ответу — ссылка на статью закона или "
                "разъяснение ФНС. Если ответа в законе нет, честно так и скажу.")
    if client:
        _path(client)
        _hints(client)
    st.caption("Например:")
    examples = (ACCOUNT_EXAMPLES[:2] + EXAMPLES[:2]) if client and client.is_npd else EXAMPLES
    cols = st.columns(2)
    for k, q in enumerate(examples):
        if cols[k % 2].button(q, key=f"ex-{k}", use_container_width=True):
            st.session_state.pending = q
            st.rerun()


def chat_page() -> None:
    sidebar()
    client = _client()
    items = _items()

    sent = st.chat_input("Спросите о налоге для самозанятых или о своих поступлениях")
    question = sent or st.session_state.pop("pending", None)
    if question and items and items[-1]["question"] == question and time.time() - items[-1].get("ts", 0) < 10:
        question = None          # двойное нажатие — тот же вопрос только что задан

    if not items and not question:
        _hero(client)
    for i, item in enumerate(items):
        with st.chat_message("user", avatar=USER_AVATAR):
            st.markdown(item["question"])
        with st.chat_message("assistant", avatar=LOGO):
            render_answer(item, i)

    if not question:
        return
    with st.chat_message("user", avatar=USER_AVATAR):
        st.markdown(question)
    with st.chat_message("assistant", avatar=LOGO):
        if not health()["llm"]:
            st.error("Сервис ответов временно недоступен. Попробуйте через минуту.", icon=":material/cloud_off:")
            return
        context = [{"question": h.get("asked") or h["question"], "text": h["text"],
                    "docs": [s["doc_id"] for s in h.get("sources", []) if s["cited"]]} for h in items[-3:]]
        holder = st.empty()
        with holder.status("Ищу ответ…") as status:
            r = get_agent().ask(question, client=client, history=context or None,
                                user=st.session_state.get("client_id") or "demo",
                                on_step=lambda msg: status.update(label=msg))
        holder.empty()
        item = {"question": question, "asked": r.asked, "route": r.route, "status": r.status, "text": r.text,
                "sources": r.sources, "columns": r.columns, "rows": r.rows, "seconds": r.seconds,
                "options": r.options, "ts": time.time()}
        items.append(item)
        render_answer(item, len(items) - 1)
