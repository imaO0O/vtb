"""REST API помощника — для встраивания в мобильное приложение и чат поддержки банка.

    python scripts/run_api.py                  # http://127.0.0.1:8000, документация — /docs

Доступ — по ключу в заголовке X-API-Key (переменная окружения KB_API_KEY на сервере; без неё API
не принимает запросы). Данные счёта передаёт сам банк: в прототипе — идентификатор демо-клиента,
в пилоте — поступления клиента из учётной системы банка. Каждый запрос пишется в общий журнал.
"""
import hmac
import os
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent import account, llm  # noqa: E402
from kbagent.agent import Agent  # noqa: E402

app = FastAPI(title="Первый чек — API", version="1.0",
              description="ИИ-помощник для начинающих самозанятых: ответы по закону о НПД и разъяснениям ФНС "
                          "со ссылкой на статью, подсказки по поступлениям на счёт клиента.")
_agent: Agent | None = None


def agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent()
    return _agent


def check_key(x_api_key: str | None = Header(None, description="Ключ доступа банка")) -> None:
    expected = os.environ.get("KB_API_KEY")
    if not expected:
        raise HTTPException(503, "Ключ API не задан на сервере (переменная KB_API_KEY)")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "Неверный ключ доступа")


class Turn(BaseModel):
    question: str
    answer: str = ""


class AskRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=1000, examples=["Какой лимит дохода у самозанятого?"])
    client_id: str | None = Field(None, description="Демо-клиент (anya, max, liza) — для вопросов о своём счёте")
    history: list[Turn] = Field(default_factory=list, description="Предыдущие реплики диалога (до трёх)")


class Source(BaseModel):
    n: int
    label: str = Field(description="«422-ФЗ, ст. 4, ч. 2, п. 8» или «Разъяснения ФНС: «…»»")
    doc_id: str
    article: str | None = None
    clause: str | None = None
    item: str | None = None
    url: str | None = None
    text: str


class AskResponse(BaseModel):
    status: str = Field(description="answer | no_data | model_refused | unverified | clarify | info | error")
    route: str = Field(description="documents (закон) | account (счёт клиента) | help (справка)")
    text: str
    understood_as: str | None = None
    sources: list[Source] = []
    table: dict | None = Field(None, description="Операции по счёту: {columns, rows}")
    options: list[str] = Field(default_factory=list, description="Варианты уточнения или примеры вопросов")
    seconds: float


class Hint(BaseModel):
    level: str = Field(description="error | warning | info")
    title: str
    text: str
    ask: str = Field(description="Вопрос, который показать кнопкой «Подробнее»")


@app.get("/health")
def health() -> dict:
    state = llm.health()
    return {"ok": all(state.values()), "models": state}


@app.post("/ask", response_model=AskResponse, dependencies=[Depends(check_key)])
def ask(req: AskRequest) -> AskResponse:
    client = account.load_clients().get(req.client_id) if req.client_id else None
    if req.client_id and client is None:
        raise HTTPException(404, f"Нет демо-клиента {req.client_id}")
    history = [{"question": t.question, "text": t.answer} for t in req.history[-3:]]
    r = agent().ask(req.question, client=client, history=history or None, user=f"api:{req.client_id or '-'}")
    sources = [Source(n=s["n"], label=s["label"], doc_id=s["doc_id"], article=s.get("article"), clause=s.get("clause"),
                      item=s.get("item"), url=s.get("url"), text=s["text"]) for s in r.sources if s["cited"]]
    table = {"columns": r.columns, "rows": r.rows} if r.rows else None
    return AskResponse(status=r.status, route=r.route, text=r.text, understood_as=r.asked, sources=sources,
                       table=table, options=r.options, seconds=r.seconds)


@app.get("/hints/{client_id}", response_model=list[Hint], dependencies=[Depends(check_key)])
def hints(client_id: str) -> list[Hint]:
    """Подсказки для главного экрана: лимит, поступления без чека, оплата от бывшего работодателя, налог."""
    client = account.load_clients().get(client_id)
    if client is None:
        raise HTTPException(404, f"Нет демо-клиента {client_id}")
    return [Hint(**w) for w in account.warnings(client)]
