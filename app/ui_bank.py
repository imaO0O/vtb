"""Страница «Для банка»: что спрашивают клиенты, на что нет ответа, оценки, база знаний, качество, состояние.

Для банка помощник — ещё и источник спроса: сколько клиентов спрашивают о лимите и переходе в ИП
(кандидаты на расчётный счёт и эквайринг), какие темы не покрыты базой знаний (что добавить)."""
from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

from app.ui_common import ROUTE_LABELS, STATUS_LABELS, get_agent, health, index_meta
from kbagent.gaps import find_gaps
from kbagent.journal import load_audit, load_feedback

ROOT = Path(__file__).resolve().parents[1]
INTENT_LABELS = {"limit": "Остаток до лимита", "tax": "Налог за месяц", "receipts": "Поступления без чека",
                 "employer": "Оплата от бывшего работодателя", "income": "Доход за период", "overview": "Обзор счёта"}
SERVICE = {None, "", "eval", "smoke"}


def _people(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("user") not in SERVICE]


def overview(records: list[dict]) -> None:
    people = _people(records)
    asked = [r for r in people if r.get("route") != "help"]
    answered = [r for r in asked if r.get("status") == "answer"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Вопросов", len(asked))
    c2.metric("С ответом", f"{len(answered) / len(asked):.0%}" if asked else "—")
    c3.metric("По счёту клиента", sum(r.get("route") == "account" for r in asked))
    times = sorted(r.get("seconds", 0) for r in asked)
    c4.metric("Время ответа, медиана", f"{times[len(times) // 2]:.1f} с".replace(".", ",") if times else "—")
    intents = Counter(INTENT_LABELS.get(r.get("intent"), r.get("intent")) for r in asked if r.get("intent"))
    if intents:
        st.markdown("**О чём спрашивают по своему счёту** — сигнал для предложений банка (лимит → ИП и расчётный "
                    "счёт, чеки → подключение уполномоченного банка)")
        st.bar_chart(pd.Series(intents).sort_values(ascending=False), horizontal=True)
    if not asked:
        st.info("Клиенты ещё не задавали вопросов. Прогоны эталона в статистику не входят.")


def journal(records: list[dict]) -> None:
    people = _people(records)
    if not people:
        st.info("Журнал пуст.")
        return
    df = pd.DataFrame([{"Время": r.get("time"), "Клиент": r.get("client") or "—",
                        "Вопрос": r.get("question"), "Как понят": r.get("asked") or "",
                        "Источник": ROUTE_LABELS.get(r.get("route"), r.get("route")),
                        "Итог": STATUS_LABELS.get(r.get("status"), r.get("status")),
                        "Время, с": r.get("seconds"), "Ссылки": "; ".join(r.get("cited") or []),
                        "Ответ": r.get("answer")} for r in reversed(people)])
    statuses = st.multiselect("Итог", sorted(df["Итог"].unique()), placeholder="Все")
    if statuses:
        df = df[df["Итог"].isin(statuses)]
    st.dataframe(df, hide_index=True, use_container_width=True, height=420)
    st.download_button("Скачать CSV", df.to_csv(index=False).encode("utf-8-sig"), "journal.csv", "text/csv",
                       icon=":material/download:")
    st.caption("Телефоны, почта, паспорт, СНИЛС, ИНН и ФИО в журнале замаскированы; журнал хранится 180 дней.")


def gaps(records: list[dict]) -> None:
    topics, summary = find_gaps(records)
    st.caption(f"Вопросов клиентов: {summary['questions']}, без ответа: {summary['unanswered']}. "
               f"Открытых тем: {summary['topics']}, закрытых: {summary['closed']} — тема закрывается сама, когда "
               "помощник начинает на неё отвечать (например, после добавления разъяснения в базу знаний).")
    if not topics:
        st.success("Вопросов без ответа нет.")
        return
    st.dataframe(pd.DataFrame([{"Вопрос": t["question"], "Сколько раз": t["count"], "Клиентов": t["users"],
                                "Последний раз": t["last"], "Закрыта": t["closed"] or "",
                                "Другие формулировки": "; ".join(t["other"])} for t in topics]),
                 hide_index=True, use_container_width=True)


def feedback() -> None:
    items = load_feedback()
    if not items:
        st.info("Оценок пока нет.")
        return
    likes = sum(f.get("score") == "👍" for f in items)
    st.caption(f"👍 {likes} · 👎 {len(items) - likes}")
    st.dataframe(pd.DataFrame([{"Время": f.get("time"), "Оценка": f.get("score"), "Вопрос": f.get("question"),
                                "Комментарий": f.get("comment", ""), "Ответ": f.get("answer")}
                               for f in reversed(items)]), hide_index=True, use_container_width=True)


def knowledge() -> None:
    meta = index_meta()
    st.caption(f"Индекс собран {meta.get('built', '—')}: фрагментов {meta.get('chunks', '—')}, "
               f"документов {meta.get('documents', '—')}. Обновить тексты: python scripts/fetch_laws.py, "
               "пересобрать индекс: python scripts/build_index.py")
    for card in get_agent().cards.values():
        with st.expander(f"{card['title']}" + (f" — {card['edition']}" if card.get("edition") else "")):
            st.caption(f"{card['kind']} · фрагментов: {card['chunks']}")
            if card.get("url"):
                st.link_button("Открыть источник", card["url"], icon=":material/open_in_new:")
            st.write("; ".join(card["sections"][:40]))


def quality() -> None:
    path = ROOT / "eval" / "results" / "latest.md"
    if not path.exists():
        st.info("Прогона эталона ещё не было: python eval/run_eval.py")
        return
    st.markdown(path.read_text(encoding="utf-8"))


def state() -> None:
    names = {"llm": "Языковая модель GigaChat 3.1 Lightning", "embed": "Поиск по смыслу bge-m3",
             "rerank": "Переранжирование bge-reranker-v2-m3"}
    for key, ok in health().items():
        st.markdown(f"{':material/check_circle:' if ok else ':material/cancel:'} {names[key]} — "
                    f"{'работает' if ok else 'недоступна'}")
    st.caption("Все модели работают внутри контура банка (llama.cpp на 127.0.0.1); наружу запросы не уходят.")


def bank_page() -> None:
    st.markdown("### Для банка")
    records = load_audit()
    tabs = st.tabs(["Обзор", "Журнал", "Темы без ответа", "Оценки", "База знаний", "Качество", "Состояние"])
    with tabs[0]:
        overview(records)
    with tabs[1]:
        journal(records)
    with tabs[2]:
        gaps(records)
    with tabs[3]:
        feedback()
    with tabs[4]:
        knowledge()
    with tabs[5]:
        quality()
    with tabs[6]:
        state()
