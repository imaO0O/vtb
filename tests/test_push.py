"""Уведомление в момент поступления и превышение лимита: банк узнаёт о переводе первым и сразу говорит, что из него
следует по закону. Плюс пояснение о случае, которое модель получает заранее (data/cases.yaml, note)."""
from datetime import date

import pytest
from conftest import make_chunk, make_hit
from test_account import CHUNKS

from kbagent import account as A
from kbagent import answer as ANS

TODAY = date(2026, 10, 8)
FAQ_OVER = make_chunk("При поступлении суммы дохода, превышающей установленный лимит 2,4 млн.руб., налогоплательщик "
                      "утрачивает право … Налогоплательщик не вправе сформировать чек … только на часть полученного "
                      "дохода.", doc_id="faq_npd", article=None, clause=None, short="Разъяснения ФНС",
                      heading="Как сформировать чек при поступлении суммы дохода, превышающей лимит?")
ALL = CHUNKS + [FAQ_OVER]


@pytest.fixture(scope="module")
def clients():
    return A.load_clients(TODAY)


def arrive(c, k):
    """k-й демо-перевод клиента пришёл сегодня."""
    op = A.make_operation(c.incoming[k], TODAY, c.is_npd)
    return A.with_operations(c, [op]), op


def test_payment_for_work_offers_receipt_now_with_tax(clients):
    anya, op = arrive(clients["anya"], 0)                       # Полина Г., «За портрет по фото», 3 500 ₽ на карту
    n = A.notify(anya, op.key, ALL, TODAY)
    assert n["actions"][0] == "receipt" and n["deadline"] == "в момент оплаты"
    assert "140 ₽" in n["text"] and "105 ₽" in n["text"]        # 4 %, а с бонусом 3 %
    assert any(s["article"] == "14" for s in n["sources"])       # срок чека — ч. 3 ст. 14


def test_unclear_transfer_asks_and_personal_is_quiet(clients):
    anya, op = arrive(clients["anya"], 1)                       # Тимур А., «Перевод»
    n = A.notify(anya, op.key, ALL, TODAY)
    assert "это оплата за работу?" in n["title"] and n["actions"] == ["receipt", "personal"]
    anya, op = arrive(clients["anya"], 2)                       # Бабушка, «На обед»
    n = A.notify(anya, op.key, ALL, TODAY)
    assert n["actions"] == ["work"] and "не нужны" in n["text"] and not n["sources"]


def test_former_employer_and_not_registered(clients):
    anya, op = arrive(clients["anya"], 3)                       # кофейня, где Аня работала
    n = A.notify(anya, op.key, ALL, TODAY)
    assert n["level"] == "error" and "receipt" not in n["actions"]
    liza, op = arrive(clients["liza"], 0)
    assert A.notify(liza, op.key, ALL, TODAY)["actions"] == ["check"]


def test_limit_crossing_is_not_npd_income(clients):
    before = A.year_income(clients["max"], TODAY)[0]
    mx, op = arrive(clients["max"], 1)                          # 160 000 ₽ от организации — доход выходит за 2,4 млн
    assert A.crossing(mx, TODAY).key == op.key
    assert A.year_income(mx, TODAY)[0] == before                # с дня превышения — уже не доход самозанятого
    assert A.gross_income(mx, TODAY) == before + 160000
    assert all(o.key != op.key for o in A.no_receipt(mx, TODAY))   # и чек по нему не требуется
    assert not A.receipt_draft(mx, next(o for o in mx.operations if o.key == op.key), TODAY)["ok"]
    n = A.notify(mx, op.key, ALL, TODAY)
    assert n["level"] == "error" and any(s["doc_id"] == "faq_npd" for s in n["sources"])
    assert any(w["title"].startswith("Доход превысил лимит") for w in A.warnings(mx, TODAY))
    assert "утрачено" in A.answer("compare", mx, ALL, TODAY).text
    assert A.leads({"max": mx}, TODAY)[0]["signal"].startswith("лимит превышен")


def test_near_limit_payment_warns_how_much_is_left(clients):
    mx, op = arrive(clients["max"], 0)                          # 12 000 ₽ — до лимита остаётся 138 000 ₽
    n = A.notify(mx, op.key, ALL, TODAY)
    assert n["level"] == "warning" and "138 000 ₽" in n["text"]


def test_case_note_is_given_before_the_answer():
    hits = [make_hit("3. Чек … в момент расчета наличными или с использованием электронных средств платежа, "
                     "при иных формах безналичных расчетов — не позднее 9-го числа", article="14", clause="3")]
    note = ANS.case_note("Когда сформировать чек, если заплатили переводом на карту?", hits)
    assert "электронных средств платежа" in note
    assert ANS.case_note("До какого числа чек при оплате платёжным поручением на расчётный счёт?", hits) == ""
    term = [make_hit("2. Эксперимент проводится до 31 декабря 2028 года включительно.", article="1", clause="2")]
    assert "закон не говорит" in ANS.case_note("Отменят ли самозанятость?", term)
    assert ANS.wrong_case("Отменят ли самозанятость?", "Нет, самозанятость не отменят [1].")
    assert not ANS.wrong_case("Как отменить чек?", "Чек можно аннулировать [1].")
