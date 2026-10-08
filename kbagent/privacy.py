"""Персональные данные в журналах (152-ФЗ): маскирование и срок хранения.

Перед записью в журнал вопросов и в оценки маскируются телефоны, почта, паспорт, СНИЛС, ИНН человека,
дата рождения, домашний адрес и ФИО с отчеством в любом падеже и регистре — то, что клиент может вписать
в вопрос («я Иванова Анна Сергеевна, ИНН …»). Суммы, сроки, проценты и номера статей остаются — без них
журнал бесполезен для разбора ошибок.
Журнал вопросов хранится settings.log_retention_days дней.
"""
import json
import re
import time

from .config import settings

PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[почта]"),
    # +7 / 8 и десять цифр с любыми разделителями: мобильный и городской с кодом — «8 (4912) 12-34-56»
    (re.compile(r"(?<![\d+])(?:\+7|8)[\s()\-]*(?:\d[\s()\-]*){9}\d(?!\d)"), "[телефон]"),
    (re.compile(r"(?<![\d-])9\d{9}(?![\d-])"), "[телефон]"),                     # мобильный без +7: 9101234567
    (re.compile(r"(?<![\w.-])\d{2,3}-\d{2}-\d{2}(?![\w-])"), "[телефон]"),         # городской без кода: 12-34-56
    (re.compile(r"(?<!\d)\d{3}[\s-]?\d{3}[\s-]?\d{3}[\s-]?\d{2}(?![\d-])"), "[СНИЛС]"),
    (re.compile(r"серия\s*\d{2}\s?\d{2}\s*,?\s*(?:номер|№)\s*\d{6}(?!\d)", re.I), "[паспорт]"),
    (re.compile(r"(?<!\d)\d{2}\s?\d{2}\s?№?\s?\d{6}(?!\d)"), "[паспорт]"),
    (re.compile(r"(?<![\d-])\d{12}(?![\d-])"), "[ИНН]"),
    (re.compile(r"((?:дат\w*\s+рожд\w*|родил\w*|г\.\s?р\.|д\.\s?р\.)\s*:?\s*)\d{1,2}[./]\d{1,2}[./]\d{2,4}", re.I),
     r"\1[дата рождения]"),
    (re.compile(r"(?:ул\.|улица|пр-т|проспект|пер\.|переулок|ш\.|шоссе)\s*[^,;\n]{2,40},\s*(?:д\.|дом)\s*\d+\w?"
                r"(?:\s*,\s*(?:корп\.|к\.|стр\.)\s*\d+)?(?:\s*,\s*(?:кв\.|квартира)\s*\d+)?", re.I), "[адрес]"),
    (re.compile(r"(?:кв\.|квартира)\s*\d+", re.I), "[адрес]"),
]

# ФИО ищется по отчеству — оно есть почти только у имён людей: -ович/-евич, -ич (Ильич, Кузьмич), -овна/-евна/-ична
WORD = r"[А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)?"
MALE_RE = re.compile(r"[А-ЯЁа-яё]+(?:вич|ьич|мич|кич|тич)(?:а|у|ем|е)?", re.I)
# женское отчество похоже на прилагательные («ночной», «основной»), поэтому перед ним нужно известное имя
FEMALE_RE = re.compile(r"[А-ЯЁа-яё]+(?:овн|евн|ичн)(?:а|ы|е|у|ой)", re.I)
FEMALE_NAMES = ("александр алевтин алин алис алл анастаси ангелин анжел ann антонин анфис арин валентин валери "
                "варвар вер вероник виктори виолетт галин дарь диан дин евгени екатерин елен елизавет жанн зинаид "
                "зо инн ирин камил карин кир клавди ксени ларис лиди лили любов людмил маргарит марин мари марф "
                "милан надежд натали нел нин оксан ольг полин раис регин светлан снежан софи стефани тамар "
                "татьян ульян фаин эвелин элеонор эльвир юли ян").replace("ann", "анн").split()
# фамилия по типичному окончанию — чтобы не принять за фамилию слово в начале фразы («Передайте Анне Петровне»)
SURNAME_RE = re.compile(r"^[А-ЯЁа-яё]+(?:-[А-ЯЁа-яё]+)?(?:ов|ев|ёв|ин|ын|ск|цк|ой|ий|ых|их|ко|ук|юк|як|ец|ер|ман|ян|дзе|швили)"
                        r"(?:а|у|ым|ой|ом|е|ий|ая|ого|ому|им|ую|ей)?$", re.I)
STOP = {"и", "в", "на", "с", "к", "по", "о", "об", "от", "до", "для", "у", "за", "из", "при", "или", "а", "но",
        "тел", "телефон", "просит", "пишет", "звонил", "сказал", "уволен", "заболел", "пропуск", "акт"}


def _is_word(token: str) -> bool:
    return bool(re.fullmatch(WORD, token)) and token.lower() not in STOP


def _same_case(a: str, b: str) -> bool:
    style = lambda w: "upper" if w.isupper() else "title" if w[:1].isupper() else "lower"  # noqa: E731
    return style(a) == style(b)


def _patronymic(word: str, name: str) -> bool:
    """word — отчество при имени name: мужское — по окончанию; женское — при известном имени
    или когда оба слова с заглавной («Сидоровой Анне Петровне», а не «начальник ночной смены»)."""
    if not _same_case(word, name):
        return False
    if MALE_RE.fullmatch(word):
        return True
    if FEMALE_RE.fullmatch(word):
        low = name.lower()
        return any(low.startswith(n) and len(low) - len(n) <= 3 for n in FEMALE_NAMES) or word[:1].isupper()
    return False


def _mask_names(text: str) -> str:
    tokens = re.split(r"(\s+)", text)          # слова и пробелы вперемешку: чётные индексы — слова
    words = [i for i in range(0, len(tokens), 2)]
    hide: set[int] = set()
    for k, i in enumerate(words):
        word = tokens[i].strip(",.;:!?«»()\"")
        if k == 0 or not (MALE_RE.fullmatch(word) or FEMALE_RE.fullmatch(word)):
            continue
        name = words[k - 1]
        first = tokens[name].strip(",.;:!?«»()\"")
        if not _is_word(first) or re.search(r"[,.;:!?]$", tokens[name]) or not _patronymic(word, first):
            continue
        hide |= {i, name}
        before = words[k - 2] if k >= 2 else None
        after = words[k + 1] if k + 1 < len(words) else None
        prev = tokens[before].strip(",.;:!?«»()\"") if before is not None else ""
        if before is not None and _is_word(prev) and not re.search(r"[,.;:!?]$", tokens[before]) and (
                SURNAME_RE.match(prev) or (prev[:1].isupper() and k >= 3 and not re.search(r"[.!?]$", tokens[words[k - 3]]))):
            hide.add(before)
        elif after is not None and not re.search(r"[,.;:!?]$", tokens[i]):
            nxt = tokens[after].strip(",.;:!?«»()\"")
            if _is_word(nxt) and (SURNAME_RE.match(nxt) or nxt[:1].isupper()):
                hide.add(after)
    if not hide:
        return text
    out, run = [], False
    for i, tok in enumerate(tokens):
        if i in hide:
            tail = re.search(r"[,.;:!?»)\"]+$", tok)
            if not run:
                out.append("[ФИО]")
            run = not tail
            if tail:
                out.append(tail.group(0))
        elif i % 2 == 1 and run and i + 1 in hide:
            continue                            # пробел внутри ФИО
        else:
            run = False
            out.append(tok)
    return "".join(out)


def mask(text: str | None) -> str | None:
    if not text or not settings.log_mask_pii:
        return text
    for rx, label in PATTERNS:
        text = rx.sub(label, text)
    return _mask_names(text)


def prune(path, days: int | None = None) -> int:
    """Удаляет из журнала (jsonl с полем time «ГГГГ-ММ-ДД …») записи старше срока; возвращает, сколько удалено."""
    days = settings.log_retention_days if days is None else days
    if not days or not path.exists():
        return 0
    border = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    lines = path.read_text(encoding="utf-8").splitlines()
    keep = [ln for ln in lines if ln.strip() and json.loads(ln).get("time", "9999") >= border]
    if len(keep) != len([ln for ln in lines if ln.strip()]):
        path.write_text("".join(ln + "\n" for ln in keep), encoding="utf-8")
    return len(lines) - len(keep)
