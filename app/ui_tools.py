"""Страница «Чеки и сроки»: помощник не только объясняет, но и даёт сделать — отметить, что поступление было оплатой
за работу, сформировать чек через банк, увидеть ближайшие сроки и включить автоплатёж налога.

Чек и автоплатёж в прототипе — демо-действия: в пилоте банк делает их как уполномоченная кредитная организация,
которая ведёт информационный обмен с ФНС (ч. 3 ст. 3 и ч. 4 ст. 11 422-ФЗ)."""
import streamlit as st

from app.ui_common import client_state, consent, current_client, get_agent, go
from kbagent import account
from kbagent.text import law_text

LEVEL_ICONS = {"error": ":material/error:", "warning": ":material/warning:", "info": ":material/event:"}


def _law(rule: str, title: str) -> None:
    """Текст нормы, на которой держится действие."""
    refs = account.Refs(get_agent().index.chunks)
    chunk = refs.find(*account.RULES[rule])
    if chunk is not None:
        with st.expander(f"{title}: {chunk.short}, {chunk.ref}", icon=":material/gavel:"):
            st.markdown(law_text(chunk.text))


def _unconfirmed(c: "account.Client", state: dict) -> None:
    ops = account.unconfirmed(c)
    if not ops:
        return
    st.markdown("#### Это оплата за работу?")
    st.caption("Банк не уверен, что это за переводы. Оплату за работу нужно учесть как доход и сформировать чек, "
               "личный перевод — нет. От ответа зависят налог и лимит.")
    for o in ops:
        with st.container(border=True):
            st.markdown(f"**{o.day:%d.%m.%Y} · {o.payer} · {account.money(o.amount)}** — «{o.purpose}»")
            left, right, _ = st.columns([1, 1, 2])
            if left.button("Оплата за работу", key=f"work-{o.key}", icon=":material/work:"):
                state["kinds"][o.key] = "work"
                st.rerun()
            if right.button("Личный перевод", key=f"pers-{o.key}", icon=":material/favorite:"):
                state["kinds"][o.key] = "personal"
                st.rerun()


def _receipts(c: "account.Client", state: dict) -> None:
    st.markdown("#### Чеки")
    missing = account.no_receipt(c, account.today())
    employer = [o for o, _ in account.former_employer_income(c)]
    if not missing and not employer:
        st.success("По всем поступлениям за работу чеки сформированы.", icon=":material/check_circle:")
    for o in missing + employer:
        draft = account.receipt_draft(c, o)
        title = f"{o.day:%d.%m.%Y} · {o.payer} · {account.money(o.amount)}"
        with st.expander(title + ("" if draft["ok"] else " — чек не нужен"), expanded=draft["ok"],
                         icon=":material/receipt_long:" if draft["ok"] else ":material/block:"):
            for problem in draft["problems"]:
                st.warning(problem, icon=":material/info:")
            if draft["ok"]:
                st.caption(f"Срок по закону: {draft['deadline']}. Черновик заполнен по реквизитам из ч. 6 ст. 14 422-ФЗ.")
                st.table({"Реквизит": [f for f, _ in draft["fields"]], "Значение": [v for _, v in draft["fields"]]})
                if st.button("Сформировать чек через банк", key=f"rcpt-{o.key}", type="primary",
                             icon=":material/send:"):
                    state["receipts"].add(o.key)
                    st.toast("Чек сформирован (демо): в пилоте банк передаёт его в ФНС как уполномоченная "
                             "организация, а клиенту — ссылку на чек для покупателя.", icon=":material/check_circle:")
                    st.rerun()
    done = [o for o in c.operations if o.key in state["receipts"]]
    if done:
        st.caption("Сформировано в этой сессии: " + "; ".join(f"{o.day:%d.%m} — {o.payer}, "
                                                               f"{account.money(o.amount)}" for o in done))
    _law("receipt_fields", "Какие реквизиты в чеке")
    _law("bank_partner", "Почему чек может сформировать банк")


def _calendar(c: "account.Client") -> None:
    st.markdown("#### Ближайшие сроки")
    events = account.calendar_events(c)
    if not events:
        st.success("Ближайших сроков нет.", icon=":material/check_circle:")
    now = account.today()
    for k, e in enumerate(events):
        when = "Сегодня" if e["date"] == now else f"{e['date']:%d.%m.%Y}"
        cols = st.columns([5, 1], vertical_alignment="center")
        cols[0].markdown(f"{LEVEL_ICONS[e['level']]} **{when} — {e['title']}.** {e['text']}")
        if e["ask"] and cols[1].button("Подробнее", key=f"cal-{k}", type="tertiary", icon=":material/forum:"):
            go("chat", e["ask"])


def _autopay(c: "account.Client", state: dict) -> None:
    st.markdown("#### Налог")
    value = st.toggle("Банк платит налог за меня (автоплатёж)", value=c.autopay, key=f"autopay-{c.id}",
                      help="Налоговая присылает уведомление в банк, и банк платит налог в срок")
    if value != c.autopay:
        state["autopay"] = value
        st.rerun()
    t = account.month_tax(c, *account._month_shift(account.today(), 1), account.today())
    st.caption(f"Налог за прошлый месяц — около {account.money(t.payable)}; ФНС пришлёт сумму до 12-го, заплатить — "
               "до 28-го числа." + (" Заплатит банк." if c.autopay else ""))
    _law("autopay", "Почему налог может платить банк")


def tools_page() -> None:
    st.markdown("### Чеки и сроки")
    c = current_client()
    if c is None:
        st.info("Выберите демо-клиента слева — здесь появятся его поступления, чеки и сроки.", icon=":material/person:")
        return
    if not consent():
        st.warning("Анализ поступлений выключен — помощник не смотрит операции по счёту. Включите его слева, "
                   "чтобы видеть чеки, сроки и подсказки.", icon=":material/lock:")
        return
    state = client_state(c.id)
    if not c.is_npd:
        st.info(f"{c.name} пока не самозанятый. Проверьте, подходит ли режим, — а встать на учёт можно прямо в "
                "приложении банка.", icon=":material/fact_check:")
        if st.button("Подходит ли мне самозанятость?", type="primary", icon=":material/arrow_forward:"):
            go("check")
        _unconfirmed(c, state)
        _calendar(c)
        return
    _unconfirmed(c, state)
    _receipts(c, state)
    _calendar(c)
    _autopay(c, state)
