"""Проверки ответа: ссылки, нумерация, служебные строки, отказы, запреты, путаница случаев и налогов."""
from conftest import make_hit

from kbagent import answer as A


def test_ref_numbers_lists_and_ranges():
    assert A._ref_numbers("1, 3") == {1, 3}
    assert A._ref_numbers("1–3") == {1, 2, 3}
    assert A._valid_refs("ответ [1, 9] и [2–3]", 4) == [1, 2, 3]


def test_clean_refs_drops_unknown_numbers():
    assert A._clean_refs("факт [1, 9]. ещё [9].", [1]) == "факт [1]. ещё ."


def test_renumber_starts_from_one_in_order_of_appearance():
    hits = [make_hit(f"фрагмент {k}", clause=str(k)) for k in range(1, 5)]
    text, new_hits, cited = A._renumber("Первое [4]. Второе [2].", hits, [2, 4])
    assert text == "Первое [1]. Второе [2]."
    assert [h.chunk.clause for h in new_hits[:2]] == ["4", "2"] and cited == [1, 2]


def test_service_lines_are_removed_but_cited_lines_stay():
    assert A._merge_refs("налог 4 % [4] [1][3][2][4].") == "налог 4 % [1, 2, 3, 4]."
    assert A._strip_service_lines("Нельзя [1].\nОснование: [1].") == "Нельзя [1]."
    # строка, переписанная из разъяснения ФНС, — источник и так показан под ответом
    assert A._strip_service_lines("Нельзя [1].\n\n— Основание: ч. 8 ст. 2 Федерального закона.") == "Нельзя [1]."


def test_refusal_disguised_as_answer():
    assert A._is_refusal("Нет. В предоставленных фрагментах нет информации об этом [1, 2]")
    assert not A._is_refusal("Нет. Лимит дохода — 2,4 миллиона рублей в год [1].")


def test_source_labels_for_law_faq_and_card():
    law = make_hit("…", article="4", clause="2", item="8")
    faq = make_hit("…", doc_id="faq_npd", article=None, clause=None, heading="Какой срок уплаты налога?",
                   kind="Разъяснения ФНС России", short="Разъяснения ФНС")
    assert A.source_label(law) == "422-ФЗ, ст. 4, ч. 2, п. 8"
    assert A.source_label(faq) == "Разъяснения ФНС: «Какой срок уплаты налога?»"


def test_ban_must_be_in_the_fragment():
    hits = [make_hit("2. Не вправе применять специальный налоговый режим: … 2) лица, осуществляющие перепродажу "
                     "товаров;")]
    kept, dropped = A._drop_unsupported_bans("Перепродавать товары нельзя [1].", hits)
    assert dropped == 0                       # «нельзя» подтверждено «не вправе» во фрагменте
    hits = [make_hit("Налоговые ставки: 4 процента и 6 процентов.")]
    text, dropped = A._drop_unsupported_bans("Ставка 4 % [1]. Повышать ставки нельзя [1].", hits)
    assert dropped == 1 and text == "Ставка 4 % [1]."


def test_neighbouring_case_in_one_part_is_caught():
    case = A.wrong_case("Когда сформировать чек, если клиент заплатил переводом на карту?",
                        "Чек нужно сформировать не позднее 9-го числа следующего месяца [1].")
    assert case and case["name"].startswith("срок чека")
    # правило для своего случая — не путаница; «безналичной» — не «наличные»
    assert not A.wrong_case("Когда сформировать чек при оплате картой?", "В момент расчёта [1].")
    assert not A.wrong_case("До какого числа сформировать чек при иных формах безналичной оплаты?",
                            "Не позднее 9-го числа [1].")
    assert A.wrong_case("Какую ставку платить, если заказчик — ООО?", "Ставка 4 % [1].")


def test_tax_from_question_must_be_in_cited_fragment():
    hits = [make_hit("Налоговые ставки: 4 процента — физическим лицам, 6 процентов — организациям.")]
    assert A.missing_subject("Какая ставка НДС в 2026 году?", hits, [1]) == "НДС"
    assert A.missing_subject("Какие ставки у самозанятых?", hits, [1]) is None
    hits = [make_hit("Физические лица освобождаются от налогообложения налогом на доходы физических лиц.")]
    assert A.missing_subject("Освобождается ли самозанятый от НДФЛ?", hits, [1]) is None


def test_garbled_word_is_restored_from_fragment():
    hits = [make_hit("Налогоплательщик обязан сформировать чек в момент расчета.")]
    text, fixed = A._fix_garbled("Налогоплатильщик обязан сформировать чек [1].", hits, "Когда чек?")
    assert "Налогоплательщик" in text and fixed
