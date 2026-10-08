"""«Первый чек» — ИИ-помощник для начинающих самозанятых. Веб-интерфейс прототипа.

    python scripts/run_chat.py        (или scripts\\run_chat.bat)

В банковском приложении клиент уже вошёл в систему, поэтому в прототипе вместо входа — выбор демо-клиента
(data/demo/clients.yaml) и его согласие на анализ поступлений; кнопки «Пришёл перевод» показывают уведомление,
которое помощник присылает в момент поступления (app/ui_push.py). Страницы: «Помощник» (app/ui_chat.py),
«Чеки и сроки» (app/ui_tools.py), «Подходит ли мне самозанятость» (app/ui_check.py) и «Для банка»
(app/ui_bank.py) — лиды, экономика, вопросы специалисту, журнал, темы без ответа, качество.
"""
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ui_bank import bank_page  # noqa: E402
from app.ui_chat import chat_page  # noqa: E402
from app.ui_check import check_page  # noqa: E402
from app.ui_common import APP_NAME, LOGO, clients, consent, current_client, inject_css  # noqa: E402
from app.ui_push import incoming_panel, show_push  # noqa: E402
from app.ui_tools import tools_page  # noqa: E402
from kbagent import account  # noqa: E402

st.set_page_config(page_title=APP_NAME, page_icon=LOGO, layout="wide")
inject_css()
st.logo(LOGO, size="large")

demo = clients(account.today().isoformat())
NO_CLIENT = "Без данных счёта"
with st.sidebar:
    labels = {f"{c.name}, {c.age} — {'самозанятый' if c.is_npd else 'пока не самозанятый'}": cid
              for cid, c in demo.items()}
    choice = st.selectbox("Демо-клиент банка", list(labels) + [NO_CLIENT], key="client_label",
                          help="В приложении банка клиент уже вошёл: помощник видит его поступления. "
                               "«Без данных счёта» — только ответы по закону.")
    st.session_state.client_id = labels.get(choice)
    client = demo.get(st.session_state.client_id)
    if client:
        st.caption(client.about)
        st.toggle("Помощник учитывает мои поступления", key="consent", value=True,
                  help="Согласие на анализ операций по счёту: суммы поступлений, плательщики и назначения платежей. "
                       "Без него помощник отвечает только по закону. Данные не покидают банк.")
        if consent():
            incoming_panel(current_client())

pages = {
    "chat": st.Page(chat_page, title="Помощник", icon=":material/forum:", default=True),
    "tools": st.Page(tools_page, title="Чеки и сроки", icon=":material/receipt_long:", url_path="checks"),
    "check": st.Page(check_page, title="Подходит ли мне НПД", icon=":material/fact_check:", url_path="fit"),
    "bank": st.Page(bank_page, title="Для банка", icon=":material/insights:", url_path="bank"),
}
st.session_state["pages"] = pages
page = st.navigation(list(pages.values()), position="sidebar")
show_push()
page.run()
