"""Журналы, персональные данные, темы без ответа, оценка, API."""
import json


def test_personal_data_is_masked(tmp_settings):
    from kbagent.privacy import mask
    text = ("Я Иванова Анна Сергеевна, тел. +7 (912) 345-67-89, почта anna@mail.ru, паспорт 4510 123456, "
            "ИНН 771234567890. Сколько мне осталось до лимита?")
    masked = mask(text)
    for secret in ["Анна Сергеевна", "345-67-89", "anna@mail", "123456", "771234567890"]:
        assert secret not in masked
    assert "Сколько мне осталось до лимита?" in masked
    for text in ["Какой лимит 2,4 млн?", "Налог до 28 числа", "Штраф 20 процентов"]:
        assert mask(text) == text


def test_old_journal_records_are_pruned(tmp_path):
    from kbagent.privacy import prune
    log = tmp_path / "audit.jsonl"
    log.write_text("".join(json.dumps({"time": t, "question": "q"}) + "\n"
                           for t in ["2020-01-01 10:00:00", "2099-01-01 10:00:00"]), encoding="utf-8")
    assert prune(log, days=180) == 1 and "2020" not in log.read_text(encoding="utf-8")


def test_feedback_is_saved_masked(tmp_settings):
    from kbagent.journal import load_feedback, save_feedback
    save_feedback({"question": "Мой телефон +7 912 345-67-89, какой лимит?", "score": "👎"})
    saved = load_feedback()
    assert saved[0]["score"] == "👎" and "345-67-89" not in saved[0]["question"]


def test_unanswered_questions_are_grouped_into_topics():
    from kbagent.gaps import find_gaps
    rec = lambda q, status, user="anya", t="2026-10-05 10:00:00": {  # noqa: E731
        "time": t, "user": user, "role": "Клиент", "question": q, "status": status, "route": "documents"}
    records = [rec("Сколько стоит патент в Москве?", "no_data"),
               rec("Какая стоимость патента в Москве?", "no_data", user="max", t="2026-10-05 11:00:00"),
               rec("Дают ли самозанятым отсрочку от армии?", "model_refused"),
               rec("Какой лимит дохода?", "answer"),
               rec("Сколько стоит патент в Москве?", "no_data", user="eval")]       # прогон эталона — не в счёт
    topics, total = find_gaps(records)
    assert total == {"questions": 4, "unanswered": 3, "topics": 2, "closed": 0}
    assert topics[0]["count"] == 2 and topics[0]["users"] == 2


def test_eval_source_check():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
    from run_eval import covers, has_facts, source_ok
    assert covers("8–11", "9") and covers("2", "2") and not covers("1–2", "3") and covers("2.1", "2.1")
    assert has_facts("Лимит — 2,4 млн ₽ [1].", ["2,4"]) and has_facts("Ставка 6 %", ["6%|6 процент"])
    law = {"cited": True, "doc_id": "fz422", "article": "4", "clause": "2", "label": "422-ФЗ", "text": "…"}
    faq = {"cited": True, "doc_id": "faq_npd", "article": None, "clause": None,
           "label": "Разъяснения ФНС: «Какой лимит?»", "text": "Лимит 2,4 млн рублей"}
    assert source_ok([law], [{"doc": "fz422", "article": "4", "part": "2"}], ["2,4"])
    assert not source_ok([law], [{"doc": "fz422", "article": "4", "part": "3"}], ["2,4"])
    assert source_ok([faq], [{"doc": "faq_npd"}], ["2,4"])
    assert not source_ok([faq], [{"doc": "faq_npd"}], ["28"])       # в разъяснении нет факта ответа


def test_api_requires_key(monkeypatch):
    from fastapi.testclient import TestClient

    from api.server import app
    client = TestClient(app)
    monkeypatch.delenv("KB_API_KEY", raising=False)
    assert client.post("/ask", json={"question": "Какой лимит?"}).status_code == 503
    monkeypatch.setenv("KB_API_KEY", "secret")
    assert client.post("/ask", json={"question": "Какой лимит?"}).status_code == 401
    assert client.get("/hints/anya", headers={"X-API-Key": "wrong"}).status_code == 401
