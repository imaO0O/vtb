"""Разбор закона и разъяснений ФНС: номера статей, частей и пунктов сохраняются и становятся ссылкой."""
from conftest import make_chunk

from kbagent.ingest import _faq_chunks, _law_chunks, _law_units

LAW = """# Федеральный закон
Статья 4. Налогоплательщики налога на профессиональный доход
@url https://example.org/st4
1. Налогоплательщиками признаются физические лица, перешедшие на специальный налоговый режим.
2. Не вправе применять специальный налоговый режим:
1) лица, осуществляющие реализацию подакцизных товаров;
2) лица, осуществляющие перепродажу товаров;
8) налогоплательщики, у которых доходы превысили в текущем календарном году 2,4 миллиона рублей;
9) утратил силу с 1 января 2020 года. - Федеральный закон от 15.12.2019 N 428-ФЗ;

Статья 13. Налоговая декларация
@url https://example.org/st13
Налоговая декларация по налогу в налоговые органы не представляется.
"""
META = {"id": "fz422", "title": "Закон", "kind": "Федеральный закон", "access": "all", "short": "422-ФЗ",
        "part": "ч.", "item": "п."}


def test_law_keeps_article_part_and_item_numbers():
    units = _law_units(LAW.splitlines())
    assert [(u.article, u.part, u.item) for u in units] == [
        ("4", "1", None), ("4", "2", None), ("4", "2", "1"), ("4", "2", "2"), ("4", "2", "8"), ("13", None, None)]
    assert units[0].url == "https://example.org/st4"
    # «9) утратил силу» в индекс не попадает
    assert not any("утратил" in u.text for u in units)


def test_law_chunk_refs_and_lead_of_the_part():
    chunks = _law_chunks(_law_units(LAW.splitlines()), META)
    refs = [c.ref for c in chunks]
    assert refs[-1] == "ст. 13" and all(c.short == "422-ФЗ" for c in chunks)
    assert any(r.startswith("ст. 4") for r in refs)
    # пункт перечня без начала части в отдельном фрагменте получает его начало: из текста видно, что это запрет
    for c in chunks:
        if c.item and "2,4 миллиона" in c.text:
            assert "Не вправе применять" in c.text


def test_ref_formats_for_law_and_code():
    assert make_chunk("…", article="4", clause="2", item="8").ref == "ст. 4, ч. 2, п. 8"
    code = make_chunk("…", doc_id="nk_rf", article="129.13", clause="1", short="НК РФ")
    code.part_word = "п."
    assert code.ref == "ст. 129.13, п. 1"


def test_faq_question_becomes_heading_and_outdated_answers_are_skipped():
    lines = ["Вопрос: Какой срок уплаты налога?", "Не позднее 28 числа следующего месяца.",
             "Источник: ч. 3 ст. 11 Федерального закона от 27.11.2018 № 422-ФЗ",
             "Вопрос: Я самозанятый, будут ли мне оплачиваться больничные листы?", "Получить пособие невозможно."]
    meta = {"id": "faq_npd", "title": "ФНС", "kind": "Разъяснения ФНС России", "access": "all",
            "short": "Разъяснения ФНС", "url": "https://npd.nalog.ru/faq/",
            "outdated": [{"question": "Я самозанятый, будут ли мне оплачиваться больничные листы?"}]}
    chunks = _faq_chunks(lines, meta)
    assert len(chunks) == 1
    assert chunks[0].heading == "Какой срок уплаты налога?"
    assert "Основание, указанное ФНС: ч. 3 ст. 11" in chunks[0].text


def test_law_text_keeps_item_numbers_in_markdown():
    from kbagent.text import law_text
    shown = law_text("2. Не признаются объектом: …\n10) от уступки прав требований;")
    # номер не превращается в пункт нумерованного списка, а строки остаются отдельными абзацами
    assert shown.splitlines() == [r"2\. Не признаются объектом: …", "", r"10\) от уступки прав требований;"]
