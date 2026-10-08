"""Страница «Для банка»: лиды на продукты, экономика, вопросы специалисту, что спрашивают клиенты, на что нет
ответа, оценки, база знаний и её актуальность, качество, состояние.

Для банка помощник — ещё и источник спроса: сколько клиентов спрашивают о лимите и переходе в ИП
(кандидаты на расчётный счёт и эквайринг), какие темы не покрыты базой знаний (что добавить)."""
import json
from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

from app.ui_common import ROUTE_LABELS, STATUS_LABELS, clients, get_agent, health, index_meta
from kbagent import account, specialists
from kbagent.gaps import find_gaps
from kbagent.journal import load_audit, load_feedback

ROOT = Path(__file__).resolve().parents[1]
INTENT_LABELS = {"limit": "Остаток до лимита", "tax": "Налог за месяц", "receipts": "Поступления без чека",
                 "employer": "Оплата от бывшего работодателя", "income": "Доход за период", "overview": "Обзор счёта",
                 "compare": "ИП или самозанятость", "forecast": "Прогноз по лимиту", "calendar": "Мои сроки"}
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
    times = sorted(r.get("seconds", 0) for r in asked if r.get("route") == "documents")   # расчёт по счёту — мгновенно
    c4.metric("Ответ по закону, медиана", f"{times[len(times) // 2]:.1f} с".replace(".", ",") if times else "—")
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
    status_path = ROOT / "data" / "kb_status.json"
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        docs = status.get("documents", {})
        changed = [d for d in docs.values() if d.get("status") in ("изменения", "ошибка")]
        (st.warning if changed else st.success)(
            f"Сверка с действующими текстами законов и ФНС: {status.get('checked')}. "
            + ("Есть изменения — обновите тексты и пересоберите индекс." if changed else "Всё актуально."),
            icon=":material/update:")
        st.dataframe(pd.DataFrame([{"Документ": d["title"], "Статус": d["status"],
                                    "Изменения": "; ".join(d.get("changes", [])[:5])} for d in docs.values()]),
                     hide_index=True, use_container_width=True)
    else:
        st.info("Сверку с действующими текстами ещё не запускали: python scripts/check_updates.py")
    st.caption(f"Индекс собран {meta.get('built', '—')}: фрагментов {meta.get('chunks', '—')}, "
               f"документов {meta.get('documents', '—')}. Обновить тексты: python scripts/fetch_laws.py, "
               "пересобрать индекс: python scripts/build_index.py")
    for card in get_agent().cards.values():
        with st.expander(f"{card['title']}" + (f" — {card['edition']}" if card.get("edition") else "")):
            st.caption(f"{card['kind']} · фрагментов: {card['chunks']}")
            if card.get("url"):
                st.link_button("Открыть источник", card["url"], icon=":material/open_in_new:")
            st.write("; ".join(card["sections"][:40]))


def leads_tab() -> None:
    """Кому что предложить: сигналы из поступлений клиентов, давших согласие на их анализ."""
    rows = account.leads(clients(account.today().isoformat()))
    st.caption("Сигналы считаются по поступлениям только тех клиентов, кто разрешил помощнику их анализировать. "
               "Предложение показывается клиенту в помощнике как «предложение банка», отдельно от ответа по закону.")
    if not rows:
        st.info("Сигналов нет.")
        return
    st.dataframe(pd.DataFrame([{"Клиент": r["client"], "Этап": r["stage"], "Продукт": r["product"],
                                "Сигнал": r["signal"]} for r in rows]), hide_index=True, use_container_width=True)
    by_product = Counter(r["product"] for r in rows)
    st.bar_chart(pd.Series(by_product).sort_values(ascending=False), horizontal=True)


def economics_tab() -> None:
    """Экономика для банка: калькулятор, все значения — допущения, которые банк заменит своими данными."""
    st.warning("Все значения ниже — допущения для примера, а не данные банка. Замените их своими цифрами; в пилоте "
               "(месяцы 4–6) они измеряются на группе клиентов.", icon=":material/calculate:")
    left, right = st.columns(2)
    with left:
        clients_n = st.number_input("Самозанятых клиентов банка", 100, 20_000_000, 100_000, step=10_000)
        active = st.slider("Пользуются помощником хотя бы раз в месяц, %", 1, 100, 25)
        to_ip = st.slider("Из активных за год открыли ИП и расчётный счёт в банке, %", 0.0, 20.0, 2.0, step=0.5)
        ip_revenue = st.number_input("Доход банка с одного ИП в год (РКО, эквайринг, остатки), ₽", 0, 1_000_000, 25_000,
                                     step=1000)
    with right:
        contacts = st.number_input("Обращений в поддержку о самозанятости на 1000 клиентов в месяц", 0, 1000, 20)
        deflect = st.slider("Из них закрывает помощник, %", 0, 100, 40)
        contact_cost = st.number_input("Стоимость одного обращения, ₽", 0, 10_000, 150, step=10)
        costs = st.number_input("Сервер с видеокартой и сопровождение в год, ₽", 0, 100_000_000, 3_000_000,
                                step=100_000)
    def fmt(x: float) -> str:
        return f"{x:,.0f}".replace(",", " ")
    users = clients_n * active / 100
    new_ip = users * to_ip / 100
    revenue = new_ip * ip_revenue
    saved = clients_n / 1000 * contacts * 12 * deflect / 100 * contact_cost
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Активных пользователей", fmt(users))
    c2.metric("Новых ИП с расчётным счётом в год", fmt(new_ip))
    c3.metric("Доход и экономия в год", f"{(revenue + saved) / 1e6:,.1f} млн ₽".replace(".", ","))
    c4.metric("За вычетом затрат", f"{(revenue + saved - costs) / 1e6:,.1f} млн ₽".replace(".", ","))
    per_client = (revenue + saved) / clients_n if clients_n else 0
    if per_client > 0:
        st.success(f"Окупается от {fmt(costs / per_client)} самозанятых клиентов банка: при этих допущениях один "
                   f"клиент приносит около {fmt(per_client)} ₽ в год, а затраты на сервер почти не зависят от числа "
                   "клиентов.", icon=":material/trending_up:")
    st.caption(f"Доход от новых ИП = {fmt(users)} активных × {to_ip} % × {fmt(ip_revenue)} ₽; экономия поддержки = "
               f"{fmt(clients_n)} / 1000 × {contacts} обращений × 12 мес × {deflect} % × {contact_cost} ₽. Не учтены "
               "остатки на картах клиентов, которые получают оплату в банк, и выручка от клиентов, оставшихся "
               "самозанятыми.")


def specialists_tab() -> None:
    """Вопросы, на которые помощник не нашёл ответа: специалист отвечает — ответ становится частью базы знаний."""
    st.caption("Клиент нажал «Передать вопрос специалисту банка». Ответ со ссылкой на норму публикуется в базу "
               "знаний: со следующего такого вопроса помощник отвечает сам, с пометкой «Ответ специалиста банка».")
    open_items = specialists.queue("open")
    if not open_items:
        st.info("Открытых вопросов нет.", icon=":material/inbox:")
    for item in open_items:
        with st.form(f"spec-{item['id']}"):
            st.markdown(f"**{item['time']} · клиент {item.get('client') or '—'}**")
            question = st.text_input("Вопрос (можно уточнить формулировку)", item["question"])
            answer = st.text_area("Ответ простыми словами", height=120)
            basis = st.text_input("Норма-основание", placeholder="например: ч. 2 ст. 6 Федерального закона № 422-ФЗ")
            if st.form_submit_button("Опубликовать в базу знаний", type="primary", icon=":material/publish:"):
                try:
                    n = specialists.publish_answer(item["id"], question, answer, basis, index=get_agent().index)
                    st.success(f"Опубликовано: фрагментов в базе знаний — {n}. Помощник уже отвечает на этот вопрос.")
                except ValueError as e:
                    st.error(str(e))
                except Exception as e:  # noqa: BLE001 — модель векторов недоступна: ответ сохранён, индекс позже
                    st.warning(f"Ответ сохранён, но в поиск не добавлен ({e.__class__.__name__}); пересоберите "
                               "индекс: python scripts/build_index.py")
    done = specialists.published()
    if done:
        st.markdown("**Опубликованные ответы**")
        st.dataframe(pd.DataFrame([{"Вопрос": d["question"], "Ответ": d["answer"], "Основание": d["basis"]}
                                   for d in done]), hide_index=True, use_container_width=True)


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
    tabs = st.tabs(["Обзор", "Лиды", "Экономика", "Вопросы специалисту", "Темы без ответа", "Журнал", "Оценки",
                    "База знаний", "Качество", "Состояние"])
    sections = [lambda: overview(records), leads_tab, economics_tab, specialists_tab, lambda: gaps(records),
                lambda: journal(records), feedback, knowledge, quality, state]
    for tab, section in zip(tabs, sections):
        with tab:
            section()
