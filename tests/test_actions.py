"""Действия клиента и банка: тип поступления, черновик чека, календарь, лиды, «подходит ли мне», специалисты."""
import sys
from datetime import date
from pathlib import Path

import pytest

from kbagent import account as A
from kbagent import eligibility as E
from kbagent import specialists as S

TODAY = date(2026, 10, 8)


@pytest.fixture(scope="module")
def clients():
    return A.load_clients(TODAY)


def test_transfers_are_classified_or_asked():
    assert A.classify("За урок", "Наталья Б.", "person")[0] == "work"
    assert A.classify("Оплата по договору", "ООО «Ромашка»", "company")[0] == "work"
    assert A.classify("На телефон", "Папа", "person")[0] == "personal"
    assert A.classify("Спасибо!", "Кирилл Н.", "person")[0] == "unknown"     # не гадаем — спрашиваем клиента
    # зарплата от работодателя — не доход самозанятого: ни чека, ни налога
    assert A.classify("Заработная плата за сентябрь", "ООО «Ромашка»", "company")[0] == "salary"
    assert A.classify("Аванс за сентябрь", "АО «Завод»", "company")[0] == "salary"
    assert A.classify("Аванс по договору: съёмка", "АО «Завод»", "company")[0] == "work"     # предоплата за работу
    assert A.classify("Оплата по договору: дизайн", "ООО «Ромашка»", "company")[0] == "work"


def test_client_confirmation_changes_income_and_receipts(clients):
    anya = clients["anya"]
    unsure = A.unconfirmed(anya, TODAY)
    assert [o.payer for o in unsure] == ["Кирилл Н."]
    before = A.year_income(anya, TODAY)[0]
    confirmed = A.apply_overrides(anya, {unsure[0].key: "work"})
    assert A.year_income(confirmed, TODAY)[0] == before + 2000
    assert any(o.payer == "Кирилл Н." for o in A.no_receipt(confirmed, TODAY))   # оплата — значит, нужен чек
    formed = A.apply_overrides(confirmed, receipts={o.key for o in A.no_receipt(confirmed, TODAY)})
    assert A.no_receipt(formed, TODAY) == []


def test_receipt_draft_follows_law(clients):
    anya = clients["anya"]
    card = next(o for o in anya.operations if o.payer == "Ирина П.")
    draft = A.receipt_draft(anya, card, TODAY)
    names = [f for f, _ in draft["fields"]]
    assert draft["ok"] and "ИНН продавца" in names and "Специальный налоговый режим" in names
    assert dict(draft["fields"])["ИНН покупателя"].startswith("не нужен")
    employer = next(o for o, _ in A.former_employer_income(anya))
    assert not A.receipt_draft(anya, employer, TODAY)["ok"]          # оплату от бывшего работодателя чеком не оформить


def test_calendar_and_limit_pace(clients):
    _, reach = A.limit_pace(clients["max"], TODAY)
    assert reach == date(2026, 10, 27)          # 2 250 000 ₽ за 281 день — 150 000 ₽ закончатся за 19 дней
    titles = [e["title"] for e in A.calendar_events(clients["max"], TODAY)]
    assert "Лимит 2,4 млн ₽" in titles and "Уплата налога" in titles
    tax = next(e for e in A.calendar_events(clients["anya"], TODAY) if e["title"] == "Уплата налога")
    assert tax["date"] == date(2026, 10, 28) and "795" in tax["text"]
    assert A.detect_intent("Какие у меня ближайшие сроки?") == "calendar"
    assert A.detect_intent("Когда мне платить налог?") == "calendar"


def test_bank_leads_need_consent(clients):
    rows = A.leads(clients, TODAY)
    assert rows[0]["client"] == "Максим" and rows[0]["product"] == A.PRODUCTS["ip"]
    assert any(r["client"] == "Лиза" and r["product"] == A.PRODUCTS["register"] for r in rows)
    clients_no = {k: v for k, v in clients.items()}
    clients_no["max"] = A.Client(**{**clients["max"].__dict__, "consent": False})
    assert all(r["client"] != "Максим" for r in A.leads(clients_no, TODAY))


def test_eligibility_verdicts():
    base = {"citizenship": "rf", "age": "18", "activity": ["services"], "employees": False, "income": False,
            "regime": "none", "employer": False, "civil": False}
    assert E.check(base, []).status == "yes"
    assert E.check({**base, "activity": ["services", "resale"]}, []).status == "no"
    assert E.check({**base, "employees": True}, []).status == "no"
    assert E.check({**base, "age": "14"}, []).status == "conditions"
    assert E.check({**base, "regime": "special"}, []).status == "conditions"
    assert E.check({**base, "citizenship": "other"}, []).status == "no"


def test_specialist_answer_goes_to_knowledge_base(tmp_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(tmp_settings, "raw_dir", tmp_path / "raw")
    item = S.ask_specialist("Можно ли самозанятому работать с маркетплейсом? Тел. +7 912 345-67-89", "anya")
    assert "345-67-89" not in item["question"] and S.queue("open")[0]["id"] == item["id"]
    S.publish_answer(item["id"], "Можно ли самозанятому работать с маркетплейсом?", "Да, можно.",
                     "ч. 3 ст. 3 422-ФЗ")
    assert S.queue("open") == [] and S.published()[0]["question"].startswith("Можно ли")
    with pytest.raises(ValueError):
        S.publish_answer("x", "Вопрос", "", "")                       # без нормы-основания не публикуется


def test_law_update_check_finds_changed_articles():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from check_updates import diff, split_articles, split_faq
    old = split_articles("Статья 4. Налогоплательщики\n@url x\n1. Текст.\nСтатья 5. Учёт\n1. Старый текст.")
    new = split_articles("Статья 4. Налогоплательщики\n@url y\n1. Текст.\nСтатья 5. Учёт\n1. Новый текст.\n"
                         "Статья 6. Объект\n1. Доходы.")
    assert diff(old, new, "статья") == ["изменена статья: 5", "новая статья: 6"]
    assert diff(split_faq("x\nВопрос: А?\nОтвет."), split_faq("x\nВопрос: А?\nОтвет."), "разъяснение") == []
