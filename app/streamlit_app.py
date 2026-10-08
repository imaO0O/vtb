"""«Первый чек» — ИИ-помощник для начинающих самозанятых. Веб-интерфейс прототипа.

    python scripts/run_chat.py        (или scripts\\run_chat.bat)

В банковском приложении клиент уже вошёл в систему, поэтому в прототипе вместо входа — выбор демо-клиента
(data/demo/clients.yaml): помощник видит его поступления и подсказывает про лимит, чеки и налог.
Страницы: «Помощник» (app/ui_chat.py) и «Для банка» (app/ui_bank.py) — журнал, темы без ответа, качество.
"""
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ui_bank import bank_page  # noqa: E402
from app.ui_chat import chat_page  # noqa: E402
from app.ui_common import APP_NAME, LOGO, clients, inject_css  # noqa: E402
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

pages = [st.Page(chat_page, title="Помощник", icon=":material/forum:", default=True),
         st.Page(bank_page, title="Для банка", icon=":material/insights:", url_path="bank")]
st.navigation(pages, position="sidebar").run()
