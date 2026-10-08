"""Уведомление в момент поступления — помощник сам приходит к клиенту, когда на карту пришли деньги: что это
по закону, срок чека, налог с этой оплаты, лимит — и кнопка, чтобы сделать нужное сразу.

В прототипе переводы «приходят» из боковой панели (data/demo/clients.yaml, incoming); в пилоте событие
о поступлении присылает учётная система банка (API: POST /events/operation), а уведомление — push в приложении."""
import streamlit as st

from app.ui_common import client_state, consent, current_client, get_agent, go
from kbagent import account
from kbagent.text import law_text

PUSH_CSS = """
<style>
[class*="st-key-pushcard-"] { background: #FFFFFF; border: 1px solid #DCE3F2; border-left: 6px solid #2350C8;
  border-radius: 1rem; padding: .75rem 1rem .7rem; box-shadow: 0 10px 28px rgba(18, 32, 74, .14); margin-bottom: .8rem; }
[class*="st-key-pushcard-warning"] { border-left-color: #D99A00; }
[class*="st-key-pushcard-error"] { border-left-color: #D64545; }
[class*="st-key-pushcard-"] p { margin-bottom: .25rem; }
.push-app { font-size: .78rem; color: #5E6888; letter-spacing: .01em; }
</style>
"""

# действие → (подпись кнопки, значок, тип кнопки)
ACTIONS = {
    "receipt": ("Сформировать чек", ":material/receipt_long:", "primary"),
    "personal": ("Это личный перевод", ":material/favorite:", "secondary"),
    "work": ("Это оплата за работу", ":material/work:", "secondary"),
    "check": ("Подходит ли мне самозанятость?", ":material/fact_check:", "primary"),
}


def arrive(c: "account.Client", k: int) -> None:
    """Демо: пришёл k-й перевод из списка клиента — и помощник сразу присылает уведомление."""
    state = client_state(c.id)
    state["arrived"].append(k)
    op = account.make_operation(c.incoming[k], account.today(), c.is_npd)
    st.session_state.push = {"cid": c.id, "key": op.key}


def incoming_panel(c: "account.Client") -> None:
    """Боковая панель: переводы, которые можно «получить» во время показа."""
    state = client_state(c.id)
    waiting = [k for k in range(len(c.incoming)) if k not in state["arrived"]]
    if not waiting:
        return
    st.caption("Демо: пришёл перевод на карту")
    for k in waiting:
        p = c.incoming[k]
        if st.button(f"+{account.money(p['amount'])} · {p['payer']}", key=f"in-{c.id}-{k}",
                     icon=":material/payments:", use_container_width=True):
            arrive(c, k)
            st.rerun()


def _close() -> None:
    st.session_state.pop("push", None)


def _act(action: str, n: dict, c: "account.Client") -> None:
    state = client_state(c.id)
    key = n["key"]
    if action == "receipt":
        if any(o.key == key and o.kind != "work" for o in c.operations):
            state["kinds"][key] = "work"
        state["receipts"].add(key)
        _close()
        st.toast("Чек сформирован (демо): в пилоте банк передаёт его в ФНС как уполномоченная организация, "
                 "а клиенту — ссылку на чек для покупателя.", icon=":material/check_circle:")
    elif action in ("work", "personal"):
        state["kinds"][key] = action
        if action == "personal":
            _close()
            st.toast("Отметил как личный перевод — в доход и лимит не входит.", icon=":material/favorite:")
    elif action == "check":
        _close()
        go("check")
    elif action.startswith("ask:"):
        _close()
        go("chat", action[4:])
    st.rerun()


def show_push() -> None:
    """Карточка уведомления над страницей — как push в приложении банка."""
    p = st.session_state.get("push")
    if not p or p["cid"] != st.session_state.get("client_id") or not consent():
        return
    c = current_client()
    n = account.notify(c, p["key"], get_agent().index.chunks) if c else None
    if n is None:
        _close()
        return
    st.markdown(PUSH_CSS, unsafe_allow_html=True)
    with st.container(key=f"pushcard-{n['level']}"):
        with st.container(horizontal=True, wrap=False, vertical_alignment="center",
                          horizontal_alignment="distribute"):
            st.markdown('<span class="push-app">🔔 Первый чек · только что</span>', unsafe_allow_html=True,
                        width="content")
            if st.button("", key="pa-close", icon=":material/close:", type="tertiary", help="Скрыть уведомление"):
                _close()
                st.rerun()
        st.markdown(f"**{n['title']}**  \n{n['text']}")
        with st.container(horizontal=True, gap="small"):
            for k, action in enumerate(n["actions"]):
                if action in ACTIONS:
                    label, icon, kind = ACTIONS[action]
                else:          # ask:<вопрос> — короткий вопрос на кнопке, длинный — «разобрать в чате»
                    q = action[4:]
                    label, icon, kind = (q if len(q) <= 45 and not k else "Разобрать в чате"), ":material/forum:", \
                        "secondary"
                if st.button(label, key=f"pa-{k}", icon=icon, type=kind):
                    _act(action, n, c)
            if n["sources"]:
                with st.popover("Основание", icon=":material/gavel:", type="tertiary"):
                    for s in n["sources"]:
                        with st.expander(f"[{s['n']}] {s['label']}"):
                            st.markdown(law_text(s["text"]))
