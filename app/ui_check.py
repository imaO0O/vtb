"""Страница «Подходит ли мне самозанятость?» — вход в воронку для тех, кто только начинает зарабатывать:
восемь вопросов, вывод по правилам закона со ссылками и следующий шаг — встать на учёт через банк."""
import re

import streamlit as st

from app.ui_common import get_agent, go
from kbagent.eligibility import QUESTIONS, check
from kbagent.text import law_text

STATUS = {"yes": ("Подходит", "green", ":material/check_circle:"),
          "conditions": ("Подходит с условиями", "orange", ":material/rule:"),
          "no": ("Не подходит", "red", ":material/block:")}
ITEM_ICONS = {"no": ":material/block:", "condition": ":material/rule:", "ok": ":material/check_circle:"}


def _form() -> dict | None:
    answers = {}
    with st.form("eligibility"):
        for q in QUESTIONS:
            if q["kind"] == "one":
                labels = dict(q["options"])
                answers[q["id"]] = st.radio(q["text"], list(labels), format_func=labels.get, horizontal=True)
            elif q["kind"] == "many":
                labels = dict(q["options"])
                answers[q["id"]] = st.multiselect(q["text"], list(labels), format_func=labels.get,
                                                  default=["services"], placeholder="Выберите")
            else:
                answers[q["id"]] = st.toggle(q["text"])
        sent = st.form_submit_button("Проверить", type="primary", icon=":material/fact_check:")
    return answers if sent else None


def check_page() -> None:
    st.markdown("### Подходит ли мне самозанятость?")
    st.caption("Восемь вопросов — и ответ по закону: можно ли вам платить налог на профессиональный доход, с какими "
               "условиями и что делать дальше. Языковая модель здесь не участвует: правила из 422-ФЗ проверяет "
               "программа, к каждому выводу — ссылка на пункт.")
    answers = _form()
    if answers is not None:
        st.session_state.elig_answers = answers
    answers = st.session_state.get("elig_answers")
    if not answers:
        return
    verdict = check(answers, get_agent().index.chunks)
    label, color, icon = STATUS[verdict.status]
    st.badge(label, color=color, icon=icon)
    st.markdown(f"#### {verdict.title}")
    for level, text in verdict.items:
        st.markdown(f"{ITEM_ICONS[level]} {re.sub(r'(\[\d+\])', r'**\1**', text)}")
    if verdict.next_steps:
        st.markdown("**Что дальше**")
        for k, step in enumerate(verdict.next_steps, 1):
            st.markdown(f"{k}. {re.sub(r'(\[\d+\])', r'**\1**', step)}")
    with st.expander("Источники — текст закона", icon=":material/gavel:"):
        for s in verdict.sources:
            st.markdown(f"**[{s['n']}] {s['label']}**")
            st.markdown(law_text(s["text"]))
    left, right = st.columns(2)
    if verdict.status != "no":
        if left.button("Встать на учёт через банк", type="primary", icon=":material/how_to_reg:"):
            st.info("Демо: в пилоте заявление о постановке на учёт банк передаёт в ФНС от имени клиента "
                    "(ч. 5 ст. 5 422-ФЗ) — паспортные данные уже есть в банке.", icon=":material/info:")
    if right.button("Задать вопрос помощнику", icon=":material/forum:"):
        go("chat", "Как стать самозанятым и сколько платить?")
