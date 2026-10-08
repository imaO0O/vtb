"""Счёт клиента: банк видит поступления и подсказывает самозанятому, что из них следует по закону.

Помощник внутри банковского приложения знает то, чего не знает обычная нейросеть: сколько денег пришло
на карту за работу, по каким поступлениям не сформирован чек, от кого раньше приходила зарплата.
Отсюда предупреждения:
  * доход с начала года приближается к лимиту 2,4 млн ₽ — что будет при превышении и как перейти в ИП;
  * поступления за работу без чека — срок формирования чека и штраф;
  * оплата от бывшего работодателя, если с увольнения не прошло двух лет, — такой доход не облагается НПД;
  * налог за месяц — предварительный расчёт с бонусом 10 000 ₽, когда придёт уведомление и до какого числа платить;
  * клиент ещё не самозанятый, но регулярно получает оплату за занятия — как стать самозанятым.

Все суммы считает программа, а не языковая модель, а каждое правило в ответе — со ссылкой на статью
закона из базы знаний (тот же индекс, что и для обычных ответов). Демо-клиенты — data/demo/clients.yaml,
даты операций — относительно сегодняшнего дня (KB_TODAY=ГГГГ-ММ-ДД задаёт «сегодня» для проверок).
"""
import calendar
import os
import re
from dataclasses import dataclass, field
from datetime import date

import yaml

from .config import settings
from .ingest import Chunk

LIMIT = 2_400_000                # ч. 2 ст. 4 422-ФЗ, п. 8
DEDUCTION = 10_000               # ч. 1 ст. 12 422-ФЗ
RATE = {"person": 0.04, "company": 0.06}           # ст. 10 422-ФЗ
DEDUCTION_RATE = {"person": 0.01, "company": 0.02}  # ч. 2 ст. 12 422-ФЗ
MIN_TAX = 100                    # ч. 2 ст. 11: налог меньше 100 ₽ переносится на следующий месяц
WARN_SHARE = 0.8                 # с какой доли лимита предупреждать
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]
MONTHS_PREP = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе", "сентябре", "октябре",
               "ноябре", "декабре"]

# Правила, на которые опираются подсказки: (документ, статья, часть, пункт)
RULES = {
    "limit": ("fz422", "4", "2", "8"),
    "lose_right": ("fz422", "5", "19", None),
    "to_usn": ("fz422", "15", "6", None),
    "rates": ("fz422", "10", None, None),
    "deduction": ("fz422", "12", "2", None),
    "notice": ("fz422", "11", "2", None),
    "pay_until": ("fz422", "11", "3", None),
    "receipt_term": ("fz422", "14", "3", None),
    "fine": ("nk_rf", "129.13", "1", None),
    "employer": ("fz422", "6", "2", "8"),
    "register": ("fz422", "5", "4", None),
    "register_bank": ("fz422", "5", "5", None),
    "deduction_total": ("fz422", "12", "1", None),
}


@dataclass
class Operation:
    day: date
    amount: float
    payer: str
    payer_kind: str              # person | company
    channel: str                 # card | account
    kind: str                    # work | salary | personal
    purpose: str
    receipt: bool | None = None
    inn: str | None = None

    @property
    def work(self) -> bool:
        return self.kind == "work"


@dataclass
class Client:
    id: str
    name: str
    age: int
    about: str
    npd_since: date | None
    deduction_used: float
    operations: list[Operation]
    former_employers: list[dict] = field(default_factory=list)

    @property
    def is_npd(self) -> bool:
        return self.npd_since is not None


# ---------- демо-данные ----------

def today() -> date:
    value = os.environ.get("KB_TODAY")
    return date.fromisoformat(value) if value else date.today()


def _month_shift(d: date, months: int) -> tuple[int, int]:
    total = d.year * 12 + d.month - 1 - months
    return total // 12, total % 12 + 1


def _day(now: date, m: int, d: int) -> date:
    year, month = _month_shift(now, m)
    last = calendar.monthrange(year, month)[1]
    day = min(d, last, now.day if m == 0 else last)
    return date(year, month, max(day, 1))


def _fill_year(ops: list[Operation], total: float, now: date) -> list[Operation]:
    """Операции прошлых месяцев года, чтобы доход с начала года был ровно total в любой день показа."""
    have = sum(o.amount for o in ops if o.work and o.day.year == now.year)
    rest, months = total - have, now.month - 1
    if rest <= 0 or months <= 0:
        return []
    out = []
    payers = [("Свадебная съёмка", "person", "card", 0.45), ("Съёмка для бренда", "company", "account", 0.35),
              ("Портретная съёмка", "person", "card", 0.20)]
    per_month = rest / months
    for m in range(1, months + 1):
        for k, (purpose, kind, channel, share) in enumerate(payers):
            amount = round(per_month * share, -2)
            if m == months and k == len(payers) - 1:      # последняя операция добирает остаток до точной суммы
                amount = round(rest - sum(o.amount for o in out))
            payer = "ООО «Северный ветер»" if kind == "company" else f"Клиент {m}-{k + 1}"
            out.append(Operation(_day(now, m, 5 + 9 * k), amount, payer, kind, channel, "work", purpose, True,
                                 "7801234567" if kind == "company" else None))
    return out


def load_clients(now: date | None = None) -> dict[str, Client]:
    now = now or today()
    path = settings.clients_path
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))["clients"]
    clients = {}
    for c in data:
        ops = [Operation(_day(now, o["m"], o["d"]), float(o["amount"]), o["payer"], o["payer_kind"], o["channel"],
                         o["kind"], o["purpose"], o.get("receipt"), o.get("inn")) for o in c["operations"]]
        if c.get("year_work_total"):
            ops += _fill_year(ops, float(c["year_work_total"]), now)
        ops.sort(key=lambda o: o.day)
        since = c.get("npd_since_months")
        clients[c["id"]] = Client(c["id"], c["name"], c["age"], c["about"],
                                  _day(now, since, 1) if since is not None else None, float(c.get("deduction_used", 0)),
                                  ops, c.get("former_employers", []))
    return clients


# ---------- расчёты ----------

def former_employer_income(c: Client) -> list[tuple[Operation, date]]:
    """Оплата за работу от организации, которая платила клиенту зарплату, — и дата последней зарплаты.
    Двух лет с увольнения не прошло (последняя зарплата — меньше двух лет назад)."""
    out = []
    for o in c.operations:
        if not o.work or not o.inn:
            continue
        salaries = [s.day for s in c.operations if s.kind == "salary" and s.inn == o.inn and s.day <= o.day]
        if salaries and (o.day - max(salaries)).days < 730:
            out.append((o, max(salaries)))
    return out


def npd_operations(c: Client, now: date) -> list[Operation]:
    """Поступления за работу после постановки на учёт, кроме оплаты от бывшего работодателя."""
    excluded = {id(o) for o, _ in former_employer_income(c)}
    return [o for o in c.operations if o.work and id(o) not in excluded and c.npd_since and o.day >= c.npd_since
            and o.day <= now]


def year_income(c: Client, now: date) -> tuple[float, float, int]:
    """Доход с начала года: всего, из них по чекам, число поступлений."""
    ops = [o for o in npd_operations(c, now) if o.day.year == now.year]
    return sum(o.amount for o in ops), sum(o.amount for o in ops if o.receipt), len(ops)


@dataclass
class MonthTax:
    year: int
    month: int
    base: dict                   # person / company → сумма
    tax: float                   # до вычета
    deduction: float             # бонус, на который уменьшен налог
    payable: float
    deduction_left: float        # остаток бонуса после этого месяца


def month_tax(c: Client, year: int, month: int, now: date) -> MonthTax:
    """Предварительный налог за месяц: 4 % с оплаты от физлиц, 6 % — от организаций и ИП, минус бонус
    (1 % и 2 % от этих сумм, пока не израсходованы 10 000 ₽). Бонус, израсходованный в прошлых месяцах,
    считается по тем же правилам."""
    left = max(DEDUCTION - c.deduction_used, 0.0)
    ops = npd_operations(c, now)
    months = sorted({(o.day.year, o.day.month) for o in ops if (o.day.year, o.day.month) < (year, month)})
    for y, m in months:                          # бонус, использованный до этого месяца
        base = _base(ops, y, m)
        left -= min(left, sum(base[k] * DEDUCTION_RATE[k] for k in base))
    base = _base(ops, year, month)
    tax = sum(base[k] * RATE[k] for k in base)
    deduction = min(left, sum(base[k] * DEDUCTION_RATE[k] for k in base))
    return MonthTax(year, month, base, round(tax, 2), round(deduction, 2), round(tax - deduction, 2),
                    round(left - deduction, 2))


def _base(ops: list[Operation], year: int, month: int) -> dict:
    base = {"person": 0.0, "company": 0.0}
    for o in ops:
        if (o.day.year, o.day.month) == (year, month):
            base[o.payer_kind] += o.amount
    return base


def no_receipt(c: Client, now: date) -> list[Operation]:
    return [o for o in npd_operations(c, now) if o.receipt is False]


def receipt_deadline(o: Operation) -> str:
    """Срок по ч. 3 ст. 14: при оплате картой или через СБП — в момент расчёта, при оплате по реквизитам
    счёта — не позднее 9-го числа следующего месяца."""
    if o.channel == "card":
        return "в момент оплаты"
    year, month = _month_shift(o.day, -1)
    return f"до 9 {MONTHS_GEN[month - 1]}"


# ---------- оформление ----------

def money(x: float) -> str:
    """2250000 → «2 250 000 ₽», 112.5 → «112,50 ₽»."""
    if abs(x - round(x)) < 0.005:
        return f"{round(x):,}".replace(",", " ") + " ₽"
    return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(1, 'поступление', 'поступления', 'поступлений') → «1 поступление»."""
    k = n % 100
    form = many if 11 <= k <= 14 else one if k % 10 == 1 else few if 2 <= k % 10 <= 4 else many
    return f"{n} {form}"


def ddmm(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


class Refs:
    """Ссылки на статьи закона: [n] в тексте и кнопка-источник под ответом."""

    def __init__(self, chunks: list[Chunk]):
        self.chunks, self.sources, self._n = chunks, [], {}

    def find(self, doc_id: str, article: str, part: str | None, item: str | None) -> Chunk | None:
        found = [c for c in self.chunks if c.doc_id == doc_id and c.article == article
                 and (part is None or _covers(c.clause, part)) and (item is None or c.item is None
                                                                    or _covers(c.item, item))]
        if part is None:
            return found[0] if found else None      # статья целиком — её начало
        # самый узкий фрагмент: «ч. 2, п. 8–9» точнее, чем «ч. 1–2»
        return min(found, key=lambda c: (c.item is None and item is not None, len(c.text)), default=None)

    def __call__(self, rule: str) -> str:
        chunk = self.find(*RULES[rule])
        if chunk is None:
            return ""
        if chunk.id not in self._n:
            self._n[chunk.id] = len(self.sources) + 1
            label = f"{chunk.short or chunk.doc_title}, {chunk.ref}"
            self.sources.append({"n": self._n[chunk.id], "label": label, "text": chunk.text, "doc_id": chunk.doc_id,
                                 "doc_kind": chunk.doc_kind, "clause": chunk.clause, "article": chunk.article,
                                 "item": chunk.item, "url": chunk.url, "page": None, "page_end": None,
                                 "cited": True, "score": 1.0})
        return f"[{self._n[chunk.id]}]"


def _num(value: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", value))


def _covers(span: str | None, value: str) -> bool:
    if not span:
        return False
    lo, _, hi = span.partition("–")
    return _num(lo) <= _num(value) <= _num(hi or lo)


@dataclass
class AccountAnswer:
    text: str
    sources: list
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    intent: str = ""


COLUMNS = ["Дата", "От кого", "Назначение", "Сумма", "Чек"]


def _rows(ops: list[Operation], deadline: bool = False) -> tuple[list, list]:
    cols = COLUMNS + (["Срок чека по закону"] if deadline else [])
    rows = []
    for o in ops:
        row = [o.day.strftime("%d.%m.%Y"), o.payer, o.purpose, money(o.amount),
               "есть" if o.receipt else "нет" if o.receipt is False else "—"]
        if deadline:
            row.append(receipt_deadline(o))
        rows.append(row)
    return cols, rows


# ---------- ответы ----------

FIRST_PERSON_RE = re.compile(r"\b(?:я|мне|меня|мной|мой|моя|моё|мое|мои|моих|моим|моему|моего|моей|у меня)\b", re.I)
# гипотетический вопрос («что будет, если я превышу», «могу ли я») — к закону, а не к счёту
HYPOTHETICAL_RE = re.compile(r"\bесли\b|\bбуду\b|\bбудет\b|можно ли|могу ли|нужно ли|надо ли|обязан\w* ли|"
                             r"имею ли|вправе ли|как (?:мне )?(?:стать|зарегистр|перейти|открыть|оформить)", re.I)
INTENTS = [
    # «всё ли у меня в порядке с налогами» — обзор, хотя в вопросе есть слово «налог»
    ("overview", re.compile(r"вс[её] ли (?:у меня )?(?:в порядке|нормально|хорошо|ок)|что у меня (?:со?|по)\b|"
                            r"есть ли у меня (?:проблем|ошиб|долг|риск)|провер\w+ (?:мо[йиюё]|меня)", re.I)),
    ("employer", re.compile(r"бывш\w*\s+(?:работодател|мест\w* работ)|от работодател|кофейн", re.I)),
    ("receipts", re.compile(r"без чек|не пробил|не пробит|не сформирова\w*|не выбил|чек[аиов]*\b", re.I)),
    ("limit", re.compile(r"лимит|предел|2[,.]4\s*(?:млн|миллион)|до 2[,.]4|сколько (?:ещё |еще )?(?:могу|можно|осталось)\s+"
                         r"(?:за)?работать|сколько (?:ещё |еще )?(?:могу|можно) получить|не превыс|превыш", re.I)),
    ("tax", re.compile(r"налог|заплатить|платить|к уплате|бонус|вычет", re.I)),
    ("income", re.compile(r"заработал\w*|\bдоход\w*|поступлен\w*|пришл[оа]|получил\w*|выручк", re.I)),
    ("overview", re.compile(r"\bсч[её]т\w*|финанс|всё ли (?:в порядке|нормально|хорошо)|все ли (?:в порядке|нормально)|"
                            r"провер\w+|что у меня|есть ли у меня|подсказк|предупрежд|рекоменд", re.I)),
]


def detect_intent(question: str) -> str | None:
    """Вопрос о собственных деньгах клиента: «сколько мне осталось до лимита», «по каким переводам у меня нет чека».
    Без «я / мне / мой» или с «если / могу ли» вопрос — к закону."""
    if not FIRST_PERSON_RE.search(question) or HYPOTHETICAL_RE.search(question):
        return None
    for name, rx in INTENTS:
        if rx.search(question):
            return name
    return None


def answer(intent: str, c: Client, chunks: list[Chunk], now: date | None = None) -> AccountAnswer:
    now = now or today()
    ref = Refs(chunks)
    if not c.is_npd:
        out = _not_registered(c, ref, now, intent)
    else:
        out = {"limit": _limit, "tax": _tax, "receipts": _receipts, "employer": _employer, "income": _income,
               "overview": _overview}[intent](c, ref, now)
    out.sources, out.intent = ref.sources, intent
    return out


def _limit(c: Client, ref: Refs, now: date) -> AccountAnswer:
    total, with_receipts, n = year_income(c, now)
    left, share = LIMIT - total, total / LIMIT
    lines = [f"С начала {now.year} года вам пришло {money(total)} оплаты за работу ({plural(n, 'поступление', 'поступления', 'поступлений')}), "
             f"из них по сформированным чекам — {money(with_receipts)}.",
             f"Лимит дохода для самозанятых — 2,4 млн ₽ за календарный год {ref('limit')}. "
             + (f"До него осталось {money(left)} — использовано {share * 100:.0f} % лимита." if left > 0
                else f"Лимит превышен на {money(-left)}.")]
    if share >= WARN_SHARE:
        lines.append(f"\n**Внимание:** если доход превысит 2,4 млн ₽, право применять налог на профессиональный "
                     f"доход теряется со дня превышения {ref('lose_right')}. После этого есть 20 календарных дней, "
                     "чтобы перейти на упрощённую систему налогообложения: ИП — со дня снятия с учёта, "
                     f"а если вы не ИП — нужно за эти 20 дней зарегистрироваться как ИП {ref('to_usn')}.")
        lines.append("\n_Предложение банка, а не требование закона:_ откройте ИП и расчётный счёт заранее — "
                     "банк подскажет шаги и подключит эквайринг, чтобы перейти без перерыва в работе.")
    return AccountAnswer("\n".join(lines).replace(" .", "."), [])


def _tax(c: Client, ref: Refs, now: date) -> AccountAnswer:
    cur = month_tax(c, now.year, now.month, now)
    py, pm = _month_shift(now, 1)
    prev = month_tax(c, py, pm, now)
    lines = []
    for label, t, is_current in ((f"{MONTHS[pm - 1]} {py}", prev, False), (f"{MONTHS[now.month - 1]} {now.year}", cur, True)):
        if not t.base["person"] and not t.base["company"]:
            continue
        parts = []
        if t.base["person"]:
            parts.append(f"с оплаты от физлиц {money(t.base['person'])} × 4 % = {money(t.base['person'] * 0.04)}")
        if t.base["company"]:
            parts.append(f"с оплаты от организаций и ИП {money(t.base['company'])} × 6 % = "
                         f"{money(t.base['company'] * 0.06)}")
        head = f"**За {label}**" + (" (месяц ещё идёт, расчёт по поступлениям на сегодня)" if is_current else "")
        line = f"{head}: {'; '.join(parts)} {ref('rates')}."
        if t.deduction:
            line += (f" Бонус (налоговый вычет) уменьшает налог на {money(t.deduction)} — это 1 % с оплаты от физлиц "
                     f"и 2 % с оплаты от организаций, пока не израсходованы 10 000 ₽ {ref('deduction')}.")
        line += f" Итого к уплате — около {money(t.payable)}."
        if t.payable < MIN_TAX:
            line += f" Это меньше 100 ₽ — такая сумма переносится на следующий месяц {ref('notice')}."
        lines.append(line + "\n")
    if not lines:
        lines.append(f"В этом и прошлом месяце поступлений за работу нет — налога к уплате нет {ref('rates')}.")
    left = month_tax(c, now.year, now.month, now).deduction_left
    lines.append(f"Остаток бонуса — {money(left)}." if left > 0 else "Бонус 10 000 ₽ уже израсходован "
                 f"{ref('deduction_total')}, налог считается по полной ставке.")
    lines.append(f"Сумму налога ФНС пришлёт в «Мой налог» не позднее 12-го числа следующего месяца {ref('notice')}, "
                 f"заплатить нужно не позднее 28-го числа {ref('pay_until')}. За {MONTHS[pm - 1]} — "
                 f"до 28 {MONTHS_GEN[now.month - 1]}.")
    missing = no_receipt(c, now)
    if missing:
        lines.append(f"\nВ расчёте учтены все поступления за работу, но ФНС считает налог по чекам — по "
                     f"{plural(len(missing), 'поступлению', 'поступлениям', 'поступлениям')} чека ещё нет (спросите «по каким поступлениям у меня нет чека?»).")
    if former_employer_income(c):
        lines.append("Оплата от бывшего работодателя в расчёт не включена — она не облагается налогом на "
                     f"профессиональный доход {ref('employer')}.")
    lines.append("\n_Это предварительный расчёт банка; точную сумму начисляет ФНС._")
    return AccountAnswer("\n".join(lines), [])


def _receipts(c: Client, ref: Refs, now: date) -> AccountAnswer:
    missing = no_receipt(c, now)
    if not missing:
        return AccountAnswer(f"По всем поступлениям за работу чеки сформированы. Чек нужно формировать в момент "
                             f"оплаты, а при оплате по реквизитам счёта — не позднее 9-го числа следующего месяца "
                             f"{ref('receipt_term')}.", [])
    total = sum(o.amount for o in missing)
    cols, rows = _rows(missing, deadline=True)
    text = (f"Поступления за работу без чека: {plural(len(missing), 'поступление', 'поступления', 'поступлений')} "
            f"на {money(total)}:\n"
            + "\n".join(f"- {ddmm(o.day)}, {o.payer} — {money(o.amount)} ({o.purpose}); срок чека — "
                        f"{receipt_deadline(o)}" for o in missing)
            + f"\n\nЧек формируется в «Мой налог» в момент расчёта картой или через СБП, а при оплате по реквизитам "
              f"счёта — не позднее 9-го числа месяца, следующего за месяцем оплаты {ref('receipt_term')}. "
              f"За нарушение — штраф 20 % от суммы расчёта, за повторное в течение шести месяцев — в размере "
              f"всей суммы расчёта {ref('fine')}.")
    if former_employer_income(c):
        text += ("\n\nОплату от бывшего работодателя в этот список не включаю: она не облагается налогом на "
                 f"профессиональный доход {ref('employer')} (подробнее — «что с переводом от бывшего работодателя?»).")
    return AccountAnswer(text, [], cols, rows)


def _employer(c: Client, ref: Refs, now: date) -> AccountAnswer:
    found = former_employer_income(c)
    rule = (f"Доходы от бывшего работодателя не облагаются налогом на профессиональный доход, если с увольнения "
            f"прошло меньше двух лет {ref('employer')}.")
    if not found:
        return AccountAnswer(f"Среди ваших поступлений нет оплаты за работу от организаций, которые раньше платили "
                             f"вам зарплату. {rule}", [])
    lines = []
    for o, salary in found:
        lines.append(f"- {ddmm(o.day)} — {money(o.amount)} от {o.payer} ({o.purpose}). Зарплата от этой организации "
                     f"приходила до {salary.strftime('%d.%m.%Y')} — двух лет с тех пор не прошло.")
    cols, rows = _rows([o for o, _ in found])
    text = ("Есть оплата от бывшего работодателя:\n" + "\n".join(lines) + f"\n\n{rule} Значит, оформить эту оплату "
            "как доход самозанятого и сформировать по ней чек в «Мой налог» нельзя. Как оформить выплату, уточните "
            "у заказчика или в ФНС: 8 800 222-22-22.")
    return AccountAnswer(text, [], cols, rows)


def _income(c: Client, ref: Refs, now: date) -> AccountAnswer:
    total, with_receipts, n = year_income(c, now)
    month_ops = [o for o in c.operations if o.day.year == now.year and o.day.month == now.month and o.day <= now]
    work = [o for o in month_ops if o.work]
    personal = [o for o in c.operations if o.kind == "personal" and o.day.year == now.year]
    text = (f"С начала {now.year} года оплата за работу — {money(total)} "
            f"({plural(n, 'поступление', 'поступления', 'поступлений')}), по чекам — "
            f"{money(with_receipts)}. До лимита 2,4 млн ₽ {ref('limit')} осталось {money(max(LIMIT - total, 0))}.\n\n"
            f"В {MONTHS_PREP[now.month - 1]}: {money(sum(o.amount for o in work))} за работу "
            f"({plural(len(work), 'поступление', 'поступления', 'поступлений')}).")
    if personal:
        text += (f" Переводы от близких ({len(personal)} на {money(sum(o.amount for o in personal))}) доходом "
                 "не считаю — они отмечены как личные.")
    if former_employer_income(c):
        text += (" Оплату от бывшего работодателя в доход самозанятого не включаю — она не облагается налогом на "
                 f"профессиональный доход {ref('employer')}.")
    cols, rows = _rows(month_ops)
    return AccountAnswer(text, [], cols, rows)


def warnings(c: Client, now: date | None = None) -> list[dict]:
    """Подсказки на главном экране помощника: что требует внимания и какой вопрос задать, чтобы узнать подробнее."""
    now = now or today()
    out = []
    if not c.is_npd:
        lessons = [o for o in c.operations if o.work and (now - o.day).days <= 92]
        if lessons:
            out.append({"level": "info", "title": "Регулярная оплата за занятия",
                        "text": f"{plural(len(lessons), 'перевод', 'перевода', 'переводов')} на "
                                f"{money(sum(o.amount for o in lessons))} за 3 месяца. "
                                "Если это доход от репетиторства — можно встать на учёт как самозанятый и платить 4 %.",
                        "ask": "Что у меня со счётом?"})
        return out
    for o, _ in former_employer_income(c):
        out.append({"level": "error", "title": "Оплата от бывшего работодателя",
                    "text": f"{ddmm(o.day)}: {money(o.amount)} от {o.payer}. Такой доход не облагается НПД.",
                    "ask": "Что с переводом от моего бывшего работодателя?"})
    missing = no_receipt(c, now)
    if missing:
        out.append({"level": "warning", "title": "Поступления без чека",
                    "text": f"{plural(len(missing), 'поступление', 'поступления', 'поступлений')} на "
                            f"{money(sum(o.amount for o in missing))}. Штраф — 20 % от суммы.",
                    "ask": "По каким поступлениям у меня нет чека?"})
    total, _, _ = year_income(c, now)
    if total >= WARN_SHARE * LIMIT:
        out.append({"level": "warning", "title": "Доход близко к лимиту 2,4 млн ₽",
                    "text": f"С начала года {money(total)}, осталось {money(max(LIMIT - total, 0))}.",
                    "ask": "Сколько мне осталось до лимита?"})
    py, pm = _month_shift(now, 1)
    prev = month_tax(c, py, pm, now)
    if prev.payable >= MIN_TAX and now.day <= 28:
        out.append({"level": "info", "title": f"Налог за {MONTHS[pm - 1]}",
                    "text": f"Около {money(prev.payable)}, заплатить до 28 {MONTHS_GEN[now.month - 1]}.",
                    "ask": "Сколько налога мне платить?"})
    return out


def _overview(c: Client, ref: Refs, now: date) -> AccountAnswer:
    items = warnings(c, now)
    if not items:
        total, _, _ = year_income(c, now)
        return AccountAnswer(f"Всё в порядке: чеки по поступлениям сформированы, оплаты от бывших работодателей нет, "
                             f"доход с начала года — {money(total)} из 2,4 млн ₽ {ref('limit')}.", [])
    parts = []
    for w in items:
        if w["ask"].startswith("Сколько налога"):
            body = _tax(c, ref, now).text
        elif w["ask"].startswith("По каким"):
            body = _receipts(c, ref, now).text
        elif w["ask"].startswith("Сколько мне осталось"):
            body = _limit(c, ref, now).text
        else:
            body = _employer(c, ref, now).text
        parts.append(f"**{w['title']}**\n\n{body}")
    return AccountAnswer("\n\n".join(parts), [])


def _not_registered(c: Client, ref: Refs, now: date, intent: str) -> AccountAnswer:
    lessons = [o for o in c.operations if o.work and (now - o.day).days <= 92]
    text = ("Вы пока не стоите на учёте как самозанятый (плательщик налога на профессиональный доход), "
            "поэтому этого налога у вас нет.")
    if lessons:
        text += (f" За последние три месяца на карту пришло "
                 f"{plural(len(lessons), 'перевод', 'перевода', 'переводов')} с пометкой об оплате занятий — "
                 f"{money(sum(o.amount for o in lessons))}.")
    text += ("\n\nЕсли это оплата ваших услуг, с неё можно платить налог как самозанятый:\n"
             f"1. Встать на учёт — в приложении «Мой налог» или через банк {ref('register')}.\n"
             f"2. Налог — 4 % с оплаты от физлиц и 6 % — от организаций и ИП {ref('rates')}.\n"
             f"3. Бонус 10 000 ₽ уменьшает налог: фактически 3 % и 4 %, пока он не израсходован {ref('deduction')}.\n"
             f"4. Лимит дохода — 2,4 млн ₽ в год {ref('limit')}.\n\n"
             "_Предложение банка:_ встать на учёт можно прямо в приложении банка — данные паспорта заполнятся сами.")
    cols, rows = _rows(lessons) if intent in ("income", "overview") else ([], [])
    return AccountAnswer(text, [], cols, rows)


def summary(c: Client, now: date | None = None) -> str:
    """Короткая справка о клиенте для подсказки модели и журнала (без сумм по операциям)."""
    now = now or today()
    if not c.is_npd:
        return f"{c.name}, {c.age} лет, пока не самозанятый."
    total, _, _ = year_income(c, now)
    return f"{c.name}, {c.age} лет, самозанятый с {c.npd_since:%m.%Y}, доход с начала года {money(total)}."

