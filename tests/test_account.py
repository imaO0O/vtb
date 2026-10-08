"""Счёт клиента: суммы считает программа, правила — со ссылкой на статью закона."""
from datetime import date

import pytest
from conftest import make_chunk

from kbagent import account as A

TODAY = date(2026, 10, 8)

# фрагменты закона, на которые ссылаются подсказки (тексты сокращены)
CHUNKS = [
    make_chunk("2. Не вправе применять: … 8) доходы превысили 2,4 миллиона рублей", article="4", clause="2", item="7–9"),
    make_chunk("19. Лицо считается утратившим право…", article="5", clause="18–19"),
    make_chunk("6. Лица, утратившие право…, вправе уведомить о переходе на УСН в течение 20 календарных дней",
               article="15", clause="6"),
    make_chunk("Налоговые ставки: 4 процента, 6 процентов", article="10", clause=None),
    make_chunk("1. … вычет не более 10 000 рублей. 2. … 1 процента … 2 процентов", article="12", clause="1–2", item="1"),
    make_chunk("2) … 2 процентов", article="12", clause="2", item="2"),
    make_chunk("2. … не позднее 12-го числа … менее 100 рублей", article="11", clause="2"),
    make_chunk("3. Уплата налога не позднее 28-го числа", article="11", clause="3–4"),
    make_chunk("3. Чек … в момент расчета … не позднее 9-го числа", article="14", clause="3"),
    make_chunk("1. … штраф 20 процентов", doc_id="nk_rf", article="129.13", clause="1", short="НК РФ"),
    make_chunk("2. Не признаются объектом: … 8) … работодатели менее двух лет назад", article="6", clause="2",
               item="7–12"),
    make_chunk("4. Заявление … через «Мой налог». 5. … кредитную организацию", article="5", clause="4–5"),
]


@pytest.fixture(scope="module")
def clients():
    return A.load_clients(TODAY)


def test_money_and_plural():
    assert A.money(2250000) == "2 250 000 ₽" and A.money(112.5) == "112,50 ₽"
    assert A.plural(1, "поступление", "поступления", "поступлений") == "1 поступление"
    assert A.plural(3, "поступление", "поступления", "поступлений") == "3 поступления"
    assert A.plural(11, "поступление", "поступления", "поступлений") == "11 поступлений"


def test_year_income_does_not_depend_on_the_day_of_demo(clients):
    total, with_receipts, _ = A.year_income(clients["max"], TODAY)
    assert total == 2_250_000 and with_receipts == 2_235_000
    later = A.load_clients(date(2026, 12, 20))["max"]
    assert A.year_income(later, date(2026, 12, 20))[0] == 2_250_000


def test_tax_with_bonus(clients):
    sep = A.month_tax(clients["anya"], 2026, 9, TODAY)
    # 10 500 × 4 % + 12 000 × 6 % = 1 140; бонус 1 % и 2 % = 345
    assert sep.tax == 1140 and sep.deduction == 345 and sep.payable == 795
    # бонус Максима израсходован — налог по полной ставке
    assert A.month_tax(clients["max"], 2026, 10, TODAY).payable == 6000


def test_former_employer_income_is_not_npd(clients):
    found = A.former_employer_income(clients["anya"])
    assert [o.amount for o, _ in found] == [18000]
    assert all(o.amount != 18000 for o in A.no_receipt(clients["anya"], TODAY))


def test_receipt_deadline_depends_on_payment_method(clients):
    card = next(o for o in clients["anya"].operations if o.payer == "Ирина П.")
    assert A.receipt_deadline(card) == "в момент оплаты"
    bank = A.Operation(date(2026, 10, 5), 1000, "ООО", "company", "account", "work", "по договору", False)
    assert A.receipt_deadline(bank) == "до 9 ноября"


def test_intent_needs_first_person_and_no_hypothesis():
    assert A.detect_intent("Сколько мне осталось до лимита?") == "limit"
    assert A.detect_intent("По каким поступлениям у меня нет чека?") == "receipts"
    assert A.detect_intent("Всё ли у меня в порядке с налогами?") == "overview"
    assert A.detect_intent("Что с переводом от моего бывшего работодателя?") == "employer"
    assert A.detect_intent("Какой лимит дохода у самозанятого?") is None          # о законе, не о себе
    assert A.detect_intent("Что будет, если я превышу лимит?") is None            # гипотетический — к закону


def test_answers_cite_law(clients):
    limit = A.answer("limit", clients["max"], CHUNKS, TODAY)
    assert "150 000 ₽" in limit.text and "20 календарных дней" in limit.text
    assert [s["label"] for s in limit.sources][:1] == ["422-ФЗ, ст. 4, ч. 2, п. 7–9"]
    receipts = A.answer("receipts", clients["anya"], CHUNKS, TODAY)
    assert "4 500 ₽" in receipts.text and any(s["doc_id"] == "nk_rf" for s in receipts.sources)
    assert receipts.rows and receipts.columns[-1] == "Срок чека по закону"
    liza = A.answer("tax", clients["liza"], CHUNKS, TODAY)
    assert "не стоите на учёте" in liza.text


def test_warnings_for_home_screen(clients):
    titles = [w["title"] for w in A.warnings(clients["anya"], TODAY)]
    assert titles[:2] == ["Оплата от бывшего работодателя", "Поступления без чека"]
    assert "Доход близко к лимиту 2,4 млн ₽" in [w["title"] for w in A.warnings(clients["max"], TODAY)]


def test_npd_or_ip_comparison(clients):
    r = A.regimes(clients["max"], TODAY)
    # ИП на УСН «доходы»: 6 % от 2 250 000 = 135 000; взносы 57 390 + 1 % от 1 950 000 = 76 890 уменьшают налог полностью
    assert r.usn_tax == 135_000 and r.contributions == 76_890 and r.usn_payable == 58_110 and r.ip_total == 135_000
    assert r.npd < r.ip_total
    text = A.answer("compare", clients["max"], CHUNKS, TODAY).text
    assert "135 000 ₽" in text and "57 390 ₽" in text and "150 000 ₽" in text


def test_limit_forecast_uses_account(clients):
    assert A.detect_intent("Хватит ли мне лимита, если я заработаю ещё 200 тысяч?") == "forecast"
    assert A.planned_amount("если придёт ещё 300к") == 300_000
    assert A.planned_amount("Сколько мне осталось до лимита 2,4 млн?") is None
    text = A.answer("forecast", clients["max"], CHUNKS, TODAY, question="если заработаю ещё 200 тысяч").text
    assert "превышен на 50 000 ₽" in text


def test_client_path_stage(clients):
    assert [A.stage(clients[c], TODAY) for c in ("liza", "anya", "max")] == [0, 1, 2]
    assert A.detect_intent("Что мне выгоднее: ИП или самозанятость?") == "compare"
