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
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

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
    "usn_rate": ("nk_rf", "346.20", "1", None),
    "usn_reduce": ("nk_rf", "346.21", "3.1", None, "без установленного"),
    "ip_contrib": ("nk_rf", "430", "1.2", None),
    "usn_notice": ("nk_rf", "346.13", "2", None),
    "usn_limit": ("nk_rf", "346.13", "4", None),
    "receipt_fields": ("fz422", "14", "6", None),
    "bank_partner": ("fz422", "3", "3", None),
    "autopay": ("fz422", "11", "4", None),
    "over_limit": ("faq_npd", None, None, None, "только на часть полученного дохода"),
}

# ИП на УСН «доходы» без работников (НК РФ): ставка 6 % (п. 1 ст. 346.20, регион может снизить), налог уменьшается
# на взносы ИП за себя без ограничения (п. 3.1 ст. 346.21); взносы 2026 года — 57 390 ₽ и 1 % с дохода свыше
# 300 000 ₽ (п. 1.2 ст. 430)
USN_RATE = 0.06
IP_FIXED = {2025: 53_658, 2026: 57_390, 2027: 61_154}
IP_EXTRA_RATE, IP_EXTRA_FROM = 0.01, 300_000

# Путь клиента в банке: от первого дохода к самозанятости и дальше к ИП и расчётному счёту
STAGES = ["Первый доход", "Самозанятый", "Близко к лимиту", "ИП и расчётный счёт"]


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
    reason: str = ""             # почему банк отнёс поступление к этому типу (для unknown — почему не уверен)

    @property
    def work(self) -> bool:
        return self.kind == "work"

    @property
    def key(self) -> str:
        """Устойчивый ключ операции — для подтверждений клиента и сформированных чеков."""
        return f"{self.day.isoformat()}|{self.payer}|{round(self.amount)}"


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
    full_name: str = ""          # для черновика чека (демо — вымышленные)
    inn: str = ""
    consent: bool = True         # согласие на анализ поступлений помощником (для банковских лидов)
    autopay: bool = False        # банк уполномочен платить налог (ч. 4 ст. 11 422-ФЗ)
    incoming: list[dict] = field(default_factory=list)   # демо: переводы, которые «придут» во время показа

    @property
    def is_npd(self) -> bool:
        return self.npd_since is not None


# ---------- тип поступления ----------

# Назначение платежа, по которому видно оплату за работу или личный перевод. Перевод без ясного назначения банк
# не относит ни к доходу, ни к личным — спрашивает клиента: ошибка в любую сторону — это либо лишний налог,
# либо доход без чека и штраф.
WORK_RE = re.compile(r"урок|заняти|заказ|договор|оплат|услуг|портрет|съ[её]мк|фото|дизайн|макет|логотип|иллюстрац|"
                     r"стикер|работ|консультац|ремонт|репетит|сайт|монтаж|маникюр|стрижк|тренировк|обложк|меню|"
                     r"английск|предоплат", re.I)
# зарплата и выплаты работодателя — не доход самозанятого: ни налога, ни чека. «Аванс по договору» — это
# предоплата самозанятому, поэтому аванс — только по зарплате или «за месяц»
SALARY_RE = re.compile(r"заработн\w* плат|зарплат|отпускн|больничн|пособи|расч[её]т\w* при увольнении|"
                       r"окончательн\w* расч[её]т|компенсаци\w* за отпуск|аванс\w* (?:по з|за (?:январ|феврал|март|"
                       r"апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр))", re.I)
PERSONAL_RE = re.compile(r"\bмам|\bпап|бабушк|дедушк|сестр|брат|на обед|на проезд|на телефон|подарок|день рождени|"
                         r"\bс др\b|долг|верну|займ|карманн|на кино", re.I)


def classify(purpose: str, payer: str, payer_kind: str) -> tuple[str, str]:
    """Тип поступления по назначению и плательщику: work, personal или unknown — и причина."""
    text = f"{payer} {purpose}"
    if payer_kind == "company" and SALARY_RE.search(purpose):
        return "salary", "в назначении — зарплата или выплата работодателя"
    if PERSONAL_RE.search(text):
        return "personal", "по назначению похоже на личный перевод"
    if payer_kind == "company":
        return "work", "плательщик — организация или ИП"
    if WORK_RE.search(purpose):
        return "work", "в назначении — оплата работы или услуги"
    return "unknown", "из назначения платежа не понятно, оплата это или личный перевод"


def apply_overrides(c: Client, kinds: dict | None = None, receipts: set | None = None) -> Client:
    """Клиент подтвердил тип поступления или сформировал чек — копия клиента с учётом этого."""
    kinds, receipts = kinds or {}, receipts or set()
    if not kinds and not receipts:
        return c
    ops = []
    for o in c.operations:
        kind = kinds.get(o.key, o.kind)
        receipt = True if o.key in receipts else o.receipt
        if kind == "work" and receipt is None:
            receipt = False
        reason = "подтверждено клиентом" if o.key in kinds else o.reason
        ops.append(Operation(o.day, o.amount, o.payer, o.payer_kind, o.channel, kind, o.purpose, receipt, o.inn,
                             reason))
    return replace(c, operations=ops)


def make_operation(o: dict, day: date, npd: bool) -> Operation:
    """Поступление из данных банка. Тип задан (зарплата, подтверждённый клиентом) — иначе его определяет банк по
    назначению; по оплате за работу у самозанятого чек считается не сформированным, пока клиент его не сформирует."""
    kind, reason = (o["kind"], "") if o.get("kind") else classify(o["purpose"], o["payer"], o["payer_kind"])
    receipt = o.get("receipt", False if kind == "work" and npd else None)
    return Operation(day, float(o["amount"]), o["payer"], o["payer_kind"], o["channel"], kind, o["purpose"], receipt,
                     o.get("inn"), reason)


def with_operations(c: Client, extra: list[Operation]) -> Client:
    """Клиент с новыми поступлениями — в прототипе это переводы, которые «приходят» во время показа."""
    if not extra:
        return c
    return replace(c, operations=sorted(c.operations + extra, key=lambda o: o.day))


def unconfirmed(c: Client, now: date | None = None) -> list[Operation]:
    """Поступления, про которые банк не уверен, — клиента спрашивают, оплата ли это."""
    now = now or today()
    return [o for o in c.operations if o.kind == "unknown" and o.day.year == now.year and o.day <= now]


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
        npd = c.get("npd_since_months") is not None
        ops = [make_operation(o, _day(now, o["m"], o["d"]), npd) for o in c["operations"]]
        if c.get("year_work_total"):
            ops += _fill_year(ops, float(c["year_work_total"]), now)
        ops.sort(key=lambda o: o.day)
        since = c.get("npd_since_months")
        clients[c["id"]] = Client(c["id"], c["name"], c["age"], c["about"],
                                  _day(now, since, 1) if since is not None else None, float(c.get("deduction_used", 0)),
                                  ops, c.get("former_employers", []), c.get("full_name", ""), c.get("inn", ""),
                                  c.get("consent", True), c.get("autopay", False), c.get("incoming", []))
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


def _work_income(c: Client, now: date) -> list[Operation]:
    """Поступления за работу после постановки на учёт, кроме оплаты от бывшего работодателя."""
    excluded = {id(o) for o, _ in former_employer_income(c)}
    return [o for o in c.operations if o.work and id(o) not in excluded and c.npd_since and o.day >= c.npd_since
            and o.day <= now]


def crossing(c: Client, now: date) -> Operation | None:
    """Поступление, с которым доход с начала года превысил лимит 2,4 млн ₽. Право на налог для самозанятых
    утрачено со дня превышения (ч. 19 ст. 5, п. 8 ч. 2 ст. 4 422-ФЗ), а со всей суммы этого поступления
    отчитываются по другой системе налогообложения — чек на часть суммы не формируется (разъяснение ФНС)."""
    total = 0.0
    for o in _work_income(c, now):
        if o.day.year == now.year:
            total += o.amount
            if total > LIMIT:
                return o
    return None


def over_limit(c: Client, now: date) -> list[Operation]:
    """Поступления за работу с дня превышения лимита — уже не доход самозанятого."""
    x = crossing(c, now)
    if x is None:
        return []
    ops = [o for o in _work_income(c, now) if o.day.year == now.year]
    return ops[next(k for k, o in enumerate(ops) if o is x):]


def npd_operations(c: Client, now: date) -> list[Operation]:
    """Доход самозанятого: поступления за работу после постановки на учёт, кроме оплаты от бывшего работодателя
    и поступлений с дня превышения лимита."""
    after = {id(o) for o in over_limit(c, now)}
    return [o for o in _work_income(c, now) if id(o) not in after]


def gross_income(c: Client, now: date) -> float:
    """Вся оплата за работу с начала года — вместе с поступлениями после превышения лимита."""
    return sum(o.amount for o in _work_income(c, now) if o.day.year == now.year)


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

    def __init__(self, chunks: list[Chunk], rules: dict | None = None):
        self.chunks, self.sources, self._n = chunks, [], {}
        self.rules = rules or RULES

    def find(self, doc_id: str, article: str, part: str | None, item: str | None,
             contains: str | None = None) -> Chunk | None:
        """Фрагмент со статьёй, частью и пунктом. contains — фраза, ради которой ссылаемся (у длинной части
        нужное правило может быть в её продолжении)."""
        found = [c for c in self.chunks if c.doc_id == doc_id and c.article == article
                 and (part is None or _covers(c.clause, part)) and (item is None or c.item is None
                                                                    or _covers(c.item, item))]
        if contains:
            found = [c for c in found if contains in c.text] or found
        if item is None:
            return found[0] if found else None      # статья или часть целиком — её начало
        # самый узкий фрагмент: «ч. 2, п. 8–9» точнее, чем «ч. 1–2»
        return min(found, key=lambda c: (c.item is None, len(c.text)), default=None)

    def __call__(self, rule: str) -> str:
        chunk = self.find(*self.rules[rule])
        if chunk is None:
            return ""
        if chunk.id not in self._n:
            self._n[chunk.id] = len(self.sources) + 1
            label = (f"{chunk.short or chunk.doc_title}, {chunk.ref}" if chunk.article
                     else f"{chunk.short or chunk.doc_title}: «{chunk.heading}»")
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
    # «какие у меня сроки», «когда мне платить налог» — календарь, хотя в вопросе есть слово «налог»
    ("calendar", re.compile(r"\bсрок\w*|календар\w*|напомина\w*|дедлайн\w*|ближайш\w*|"
                            r"когда (?:мне )?(?:нужно |надо )?(?:платить|заплатить|оплатить|пробить|сформировать)", re.I)),
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


COMPARE_RE = re.compile(r"\bип\b[^?]{0,40}\bили\b[^?]{0,20}самозанят|самозанят\w*[^?]{0,20}\bили\b[^?]{0,20}\bип\b|"
                        r"выгодн\w*|стоит ли (?:мне )?(?:открыва|переходи|перейти|открыть)\w*|"
                        r"(?:на|в) ип\b[^?]{0,30}(?:платить|налог|выйдет|обойд)|"
                        r"(?:платить|плачу|налог\w*|выйдет|обойд\w*)[^?]{0,20}(?:на|в) ип\b|сравни\w*", re.I)


FORECAST_RE = re.compile(r"лимит|превыш|не превыс|2[,.]4\s*(?:млн|миллион)|хватит|уложусь|влезу", re.I)
AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(млн\w*|миллион\w*|тыс\w*|т\.?\s?р\.?|к\b|000\b)?", re.I)


def planned_amount(question: str) -> float | None:
    """«ещё 200 тысяч» → 200 000, «1,5 млн» → 1 500 000, «300к» → 300 000; без суммы — None."""
    for num, unit in AMOUNT_RE.findall(question):
        value = float(num.replace(",", "."))
        unit = unit.lower()
        if unit.startswith(("млн", "миллион")):
            value *= 1_000_000
        elif unit.startswith(("тыс", "т")) or unit == "к":
            value *= 1_000
        elif unit == "000":
            value *= 1_000
        if value >= 1_000 and value != LIMIT:          # «до лимита 2,4 млн» — не плановый доход
            return value
    return None


def detect_intent(question: str) -> str | None:
    """Вопрос о собственных деньгах клиента: «сколько мне осталось до лимита», «по каким переводам у меня нет чека».
    Без «я / мне / мой» или с «если / могу ли» вопрос — к закону. «Что мне выгоднее — ИП или самозанятость?» —
    сравнение на доходе клиента, даже если спрошено в будущем времени («сколько я буду платить на ИП»)."""
    if not FIRST_PERSON_RE.search(question):
        return None
    if COMPARE_RE.search(question):
        return "compare"
    if FORECAST_RE.search(question) and planned_amount(question):
        return "forecast"            # «хватит ли мне лимита, если заработаю ещё 200 тысяч» — считаем по счёту
    if HYPOTHETICAL_RE.search(question):
        return None
    for name, rx in INTENTS:
        if rx.search(question):
            return name
    return None


def answer(intent: str, c: Client, chunks: list[Chunk], now: date | None = None,
           question: str | None = None) -> AccountAnswer:
    now = now or today()
    ref = Refs(chunks)
    if intent == "compare":
        out = _compare(c, ref, now)
    elif intent == "forecast" and c.is_npd:
        out = _forecast(c, ref, now, planned_amount(question or "") or 0)
    elif not c.is_npd:
        out = _not_registered(c, ref, now, intent)
    else:
        out = {"limit": _limit, "tax": _tax, "receipts": _receipts, "employer": _employer, "income": _income,
               "overview": _overview, "calendar": _calendar}[intent](c, ref, now)
    out.sources, out.intent = ref.sources, intent
    return out


@dataclass
class Regimes:
    income: float
    npd: float                   # налог на профессиональный доход (с остатком бонуса)
    usn_tax: float               # УСН 6 % до уменьшения
    contributions: float         # взносы ИП за себя
    usn_payable: float           # налог УСН после уменьшения на взносы
    by_kind: dict

    @property
    def ip_total(self) -> float:
        return self.usn_payable + self.contributions


def regimes(c: Client, now: date) -> Regimes:
    """Сколько бы клиент заплатил с дохода за год (с начала года до сегодня) как самозанятый и как ИП на УСН."""
    ops = [o for o in c.operations if o.work and o.day.year == now.year and o.day <= now]
    if c.is_npd:
        excluded = {id(o) for o, _ in former_employer_income(c)}
        ops = [o for o in ops if id(o) not in excluded]
    by_kind = {"person": sum(o.amount for o in ops if o.payer_kind == "person"),
               "company": sum(o.amount for o in ops if o.payer_kind == "company")}
    income = by_kind["person"] + by_kind["company"]
    bonus = max(DEDUCTION - c.deduction_used, 0.0)
    npd = sum(by_kind[k] * RATE[k] for k in by_kind) - min(bonus, sum(by_kind[k] * DEDUCTION_RATE[k] for k in by_kind))
    contributions = IP_FIXED.get(now.year, IP_FIXED[max(IP_FIXED)]) + IP_EXTRA_RATE * max(income - IP_EXTRA_FROM, 0)
    usn_tax = USN_RATE * income
    return Regimes(income, round(npd, 2), round(usn_tax, 2), round(contributions, 2),
                   round(max(usn_tax - contributions, 0), 2), by_kind)


def _exceeded(c: Client, ref: Refs, now: date, x: Operation) -> str:
    """Лимит уже превышен: что это значит по закону и разъяснению ФНС."""
    return (f"С начала {now.year} года вам пришло {money(gross_income(c, now))} оплаты за работу. Лимит для "
            f"самозанятых — 2,4 млн ₽ за календарный год {ref('limit')} — превышен {ddmm(x.day)}: поступление "
            f"{money(x.amount)} — {x.payer}.\n\nПраво применять налог на профессиональный доход утрачено со дня "
            f"превышения {ref('lose_right')}. Со всей суммы этого поступления отчитываются по другой системе "
            f"налогообложения — чек на него (и даже на часть суммы) в «Мой налог» не формируется {ref('over_limit')}. "
            "Есть 20 календарных дней, чтобы перейти на упрощённую систему налогообложения: ИП — со дня снятия "
            f"с учёта, а если вы не ИП — нужно за эти 20 дней зарегистрироваться как ИП {ref('to_usn')}.")


def _compare(c: Client, ref: Refs, now: date) -> AccountAnswer:
    r = regimes(c, now)
    x = crossing(c, now) if c.is_npd else None
    if x is not None:
        # самозанятость уже закончилась — сравнивать не с чем, считаем, сколько будет стоить ИП
        return AccountAnswer(
            _exceeded(c, ref, now, x) + "\n\n"
            f"**ИП на упрощёнке «доходы»** на вашем доходе {money(r.income)}: налог 6 % — {money(r.usn_tax)} "
            f"{ref('usn_rate')}, взносы за себя за полный год — {money(r.contributions)} {ref('ip_contrib')}; "
            f"налог уменьшается на всю сумму взносов {ref('usn_reduce')}, поэтому всего с взносами — "
            f"**{money(r.ip_total)}**.\n\n_Предложение банка:_ открыть ИП и расчётный счёт с эквайрингом можно в "
            "приложении — помощник подскажет шаги, чтобы успеть в 20 дней.", [])
    if not r.income:
        return AccountAnswer("Поступлений за работу в этом году пока нет — сравнивать не на чем. Ставки самозанятого — "
                             f"4 % и 6 % {ref('rates')}, у ИП на упрощёнке — 6 % с дохода {ref('usn_rate')} и "
                             f"обязательные взносы за себя {ref('ip_contrib')}.", [])
    year = now.year
    lines = [f"Сравнение на вашем доходе с начала {year} года — {money(r.income)}:",
             "",
             f"**Самозанятый:** налог 4 % с оплаты от физлиц и 6 % — от организаций и ИП {ref('rates')}"
             + (", минус бонус" if c.deduction_used < DEDUCTION else "") + f" — около **{money(r.npd)}**. "
             "Страховых взносов нет.",
             "",
             f"**ИП на упрощёнке «доходы»:** налог 6 % — {money(r.usn_tax)} {ref('usn_rate')}; обязательные взносы "
             f"за себя за полный год — {money(IP_FIXED.get(year, IP_FIXED[max(IP_FIXED)]))} и 1 % с дохода свыше "
             "300 000 ₽, всего "
             f"{money(r.contributions)} {ref('ip_contrib')}. ИП без работников уменьшает налог на всю сумму взносов "
             f"{ref('usn_reduce')}, поэтому налог к уплате — {money(r.usn_payable)}, а всего с взносами — "
             f"**{money(r.ip_total)}**.",
             ""]
    diff = r.ip_total - r.npd
    if diff > 0:
        lines.append(f"По расчёту самозанятость сейчас дешевле на {money(diff)}. У ИП взносы идут на пенсионное и "
                     "медицинское страхование, а доход на упрощёнке ограничен 450 млн ₽, а не 2,4 млн ₽ "
                     f"{ref('usn_limit')}.")
    else:
        lines.append(f"На вашем доходе ИП обходится не дороже самозанятости (разница {money(-diff)}), а взносы идут "
                     "на пенсионное и медицинское страхование.")
    total, _, _ = year_income(c, now)
    if c.is_npd and total >= WARN_SHARE * LIMIT:
        lines.append(f"До лимита самозанятого осталось {money(max(LIMIT - total, 0))} {ref('limit')}. Если доход его "
                     f"превысит, на упрощёнку нужно перейти в течение 20 календарных дней {ref('to_usn')}.")
    else:
        lines.append(f"Новый ИП может перейти на упрощёнку, уведомив налоговую не позднее 30 дней с регистрации "
                     f"{ref('usn_notice')}.")
    lines.append("\n_Расчёт банка без региональных льгот: регион может снизить ставку УСН. Предложение банка:_ "
                 "открыть ИП и расчётный счёт с эквайрингом можно в приложении — помощник подскажет шаги.")
    return AccountAnswer("\n".join(lines), [])


def _forecast(c: Client, ref: Refs, now: date, extra: float) -> AccountAnswer:
    """«Хватит ли мне лимита, если заработаю ещё 200 тысяч?» — доход с начала года плюс плановая сумма."""
    if crossing(c, now) is not None:
        return _limit(c, ref, now)
    total, _, _ = year_income(c, now)
    after = total + extra
    lines = [f"С начала {now.year} года вам пришло {money(total)} оплаты за работу. Вместе с ещё {money(extra)} "
             f"будет {money(after)}. Лимит для самозанятых — 2,4 млн ₽ за календарный год {ref('limit')}."]
    if after <= LIMIT:
        lines.append(f"Лимита хватит: после этой суммы останется {money(LIMIT - after)}.")
    else:
        lines.append(f"**Не хватит:** лимит будет превышен на {money(after - LIMIT)}; до него сейчас осталось "
                     f"{money(max(LIMIT - total, 0))}. Право применять налог на профессиональный доход теряется со дня "
                     f"превышения {ref('lose_right')}, после этого есть 20 календарных дней, чтобы перейти на "
                     f"упрощёнку {ref('to_usn')}.")
        lines.append("\n_Предложение банка:_ если такой доход ожидается, откройте ИП и расчётный счёт заранее. Сколько "
                     "вы будете платить как ИП, покажет вопрос «Что мне выгоднее: ИП или самозанятость?».")
    return AccountAnswer("\n".join(lines), [])


def stage(c: Client, now: date | None = None) -> int:
    """Этап пути клиента (индекс в STAGES): 0 — получает первые деньги за работу, 1 — самозанятый,
    2 — доход близко к лимиту (пора думать об ИП). Этап 3 — ИП с расчётным счётом — следующий шаг."""
    now = now or today()
    if not c.is_npd:
        return 0
    total, _, _ = year_income(c, now)
    return 2 if total >= WARN_SHARE * LIMIT or crossing(c, now) else 1


NEXT_STEP = {0: ("Как стать самозанятым и сколько платить?", "Как зарегистрироваться самозанятым?"),
             1: ("Что проверить по счёту?", "Всё ли у меня в порядке с налогами?"),
             2: ("Пора думать об ИП", "Что мне выгоднее: ИП или самозанятость?")}


def _limit(c: Client, ref: Refs, now: date) -> AccountAnswer:
    x = crossing(c, now)
    if x is not None:
        return AccountAnswer(_exceeded(c, ref, now, x) + "\n\n_Предложение банка:_ открыть ИП и расчётный счёт "
                             "можно в приложении. Сколько вы будете платить как ИП, покажет вопрос «Что мне выгоднее: "
                             "ИП или самозанятость?».", [])
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
                     "банк подскажет шаги и подключит эквайринг, чтобы перейти без перерыва в работе. Сколько вы "
                     "будете платить как ИП, покажет вопрос «Что мне выгоднее: ИП или самозанятость?».")
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
    x = crossing(c, now)
    if x is not None:
        out.append({"level": "error", "title": "Доход превысил лимит 2,4 млн ₽",
                    "text": f"{ddmm(x.day)}: {money(x.amount)} — {x.payer}. С этого дня право на налог для самозанятых "
                            "утрачено; чек на эту оплату не формируется, есть 20 дней на переход на упрощёнку.",
                    "ask": "Сколько мне осталось до лимита?"})
    elif total >= WARN_SHARE * LIMIT:
        _, reach = limit_pace(c, now)
        out.append({"level": "warning", "title": "Доход близко к лимиту 2,4 млн ₽",
                    "text": f"С начала года {money(total)}, осталось {money(max(LIMIT - total, 0))}."
                            + (f" При нынешнем темпе лимит закончится около {ddmm(reach)}." if reach else ""),
                    "ask": "Сколько мне осталось до лимита?"})
    py, pm = _month_shift(now, 1)
    prev = month_tax(c, py, pm, now)
    if prev.payable >= MIN_TAX and now.day <= 28:
        out.append({"level": "info", "title": f"Налог за {MONTHS[pm - 1]}",
                    "text": f"Около {money(prev.payable)}, заплатить до 28 {MONTHS_GEN[now.month - 1]}"
                            + (" — заплатит банк по автоплатежу." if c.autopay else "."),
                    "ask": "Сколько налога мне платить?"})
    return out + _unconfirmed_hint(c, now)


def _unconfirmed_hint(c: Client, now: date) -> list[dict]:
    ops = unconfirmed(c, now)
    if not ops:
        return []
    return [{"level": "info", "title": "Это оплата за работу?",
             "text": f"{plural(len(ops), 'поступление', 'поступления', 'поступлений')} без понятного назначения: "
                     + "; ".join(f"{ddmm(o.day)}, {o.payer}, {money(o.amount)}" for o in ops[:3])
                     + ". Отметьте в разделе «Чеки и сроки» — от этого зависят налог и лимит.",
             "ask": None}]


def _overview(c: Client, ref: Refs, now: date) -> AccountAnswer:
    items = warnings(c, now)
    if not items:
        total, _, _ = year_income(c, now)
        return AccountAnswer(f"Всё в порядке: чеки по поступлениям сформированы, оплаты от бывших работодателей нет, "
                             f"доход с начала года — {money(total)} из 2,4 млн ₽ {ref('limit')}.", [])
    parts = []
    for w in items:
        if not w["ask"]:
            body = w["text"]
        elif w["ask"].startswith("Сколько налога"):
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


# ---------- действия: чек, сроки, темп дохода ----------

def mask_inn(inn: str) -> str:
    return f"•••• {inn[-4:]}" if inn else "—"


def receipt_draft(c: Client, o: Operation, now: date | None = None) -> dict:
    """Черновик чека по реквизитам ч. 6 ст. 14 422-ФЗ. Сформировать его может банк, если клиент уполномочил банк
    на информационный обмен с ФНС (ч. 3 ст. 3) — в прототипе это демо-действие. Оплату от бывшего работодателя
    чеком оформить нельзя: такой доход не облагается налогом на профессиональный доход."""
    now = now or today()
    employer = {id(x) for x, _ in former_employer_income(c)}
    problems = []
    if id(o) in employer:
        problems.append("Это оплата от бывшего работодателя: такой доход не облагается налогом на профессиональный "
                        "доход, чек по нему не формируется.")
    if not c.is_npd:
        problems.append("Вы пока не стоите на учёте как самозанятый — сначала нужно встать на учёт.")
    if any(x is o for x in over_limit(c, now)):
        problems.append("С этой оплатой доход превысил лимит 2,4 млн ₽: со всей её суммы отчитываются по другой "
                        "системе налогообложения, чек в «Мой налог» не формируется.")
    if o.payer_kind == "company" and not o.inn:
        problems.append("Покупатель — организация или ИП: в чеке нужен его ИНН. Попросите его у заказчика.")
    fields = [
        ("Наименование документа", "Чек"),
        ("Дата и время расчёта", f"{o.day:%d.%m.%Y}"),
        ("Продавец", c.full_name or c.name),
        ("ИНН продавца", mask_inn(c.inn)),
        ("Специальный налоговый режим", "Налог на профессиональный доход"),
        ("Работы, услуги", o.purpose),
        ("Сумма расчёта", money(o.amount)),
        ("ИНН покупателя", o.inn if o.payer_kind == "company" and o.inn else
         "не нужен — покупатель физическое лицо" if o.payer_kind == "person" else "нужен"),
        ("Дата и время формирования", f"{now:%d.%m.%Y}"),
        ("Банк, сформировавший чек", "банк-партнёр (если клиент его уполномочил)"),
        ("Номер чека", "присвоит ФНС при формировании"),
    ]
    return {"fields": fields, "problems": problems, "deadline": receipt_deadline(o), "ok": not problems}


def limit_pace(c: Client, now: date | None = None) -> tuple[float, date | None]:
    """Средний доход за день с начала года (или с постановки на учёт) и дата, когда при таком темпе доход
    дойдёт до лимита 2,4 млн ₽, — если это случится в этом году."""
    now = now or today()
    x = crossing(c, now)
    total = gross_income(c, now) if x else year_income(c, now)[0]
    start = max(date(now.year, 1, 1), c.npd_since or date(now.year, 1, 1))
    days = max((now - start).days + 1, 1)
    per_day = total / days
    if x is not None:
        return per_day, x.day
    if per_day <= 0 or total >= LIMIT:
        return per_day, (now if total >= LIMIT else None)
    reach = now + timedelta(days=int((LIMIT - total) / per_day) + 1)
    return per_day, reach if reach.year == now.year else None


def calendar_events(c: Client, now: date | None = None) -> list[dict]:
    """Ближайшие сроки и события самозанятого: чеки, уведомление и уплата налога, лимит, вопросы банка.
    Каждое событие — с правилом закона (ключ RULES) и вопросом, который помощник разберёт подробнее."""
    now = now or today()
    events = []
    for o in unconfirmed(c, now):
        events.append({"date": now, "level": "info", "title": "Уточните поступление",
                       "text": f"{ddmm(o.day)}: {money(o.amount)} — {o.payer} («{o.purpose}»). Это оплата за работу "
                               "или личный перевод?", "rule": None, "ask": None})
    if not c.is_npd:
        return events
    for o in no_receipt(c, now):
        if o.channel == "card":
            when, text = now, f"{ddmm(o.day)}, {o.payer}, {money(o.amount)}: чек нужно было сформировать в момент оплаты"
            level = "error"
        else:
            year, month = _month_shift(o.day, -1)
            when, level = date(year, month, 9), "warning"
            text = f"{ddmm(o.day)}, {o.payer}, {money(o.amount)}: сформировать чек до {ddmm(when)}"
        events.append({"date": when, "level": level, "title": "Чек", "text": text, "rule": "receipt_term",
                       "ask": "По каким поступлениям у меня нет чека?"})
    py, pm = _month_shift(now, 1)
    prev = month_tax(c, py, pm, now)
    if prev.payable >= MIN_TAX and now.day <= 28:
        if now.day <= 12:
            events.append({"date": date(now.year, now.month, 12), "level": "info", "title": "Уведомление ФНС",
                           "text": f"ФНС пришлёт в «Мой налог» сумму налога за {MONTHS[pm - 1]}", "rule": "notice",
                           "ask": "Сколько налога мне платить?"})
        events.append({"date": date(now.year, now.month, 28), "level": "warning", "title": "Уплата налога",
                       "text": f"Налог за {MONTHS[pm - 1]} — около {money(prev.payable)}"
                               + (" (заплатит банк по автоплатежу)" if c.autopay else ""),
                       "rule": "pay_until", "ask": "Сколько налога мне платить?"})
    cur = month_tax(c, now.year, now.month, now)
    ny, nm = _month_shift(now, -1)
    if cur.payable > 0:
        events.append({"date": date(ny, nm, 28), "level": "info", "title": "Налог за текущий месяц",
                       "text": f"За {MONTHS[now.month - 1]} пока около {money(cur.payable)}, заплатить до "
                               f"28 {MONTHS_GEN[nm - 1]}", "rule": "pay_until", "ask": "Сколько налога мне платить?"})
    total, _, _ = year_income(c, now)
    _, reach = limit_pace(c, now)
    x = crossing(c, now)
    if x is not None:
        events.append({"date": x.day, "level": "error", "title": "Лимит 2,4 млн ₽ превышен",
                       "text": f"{money(x.amount)} — {x.payer}: право на налог для самозанятых утрачено; 20 календарных "
                               "дней на переход на упрощёнку", "rule": "to_usn",
                       "ask": "Что мне выгоднее: ИП или самозанятость?"})
    elif total >= WARN_SHARE * LIMIT or reach:
        text = f"С начала года {money(total)}, до лимита {money(max(LIMIT - total, 0))}"
        if reach:
            text += f"; при нынешнем темпе лимит закончится около {ddmm(reach)}"
        events.append({"date": reach or now, "level": "warning", "title": "Лимит 2,4 млн ₽", "text": text,
                       "rule": "limit", "ask": "Что мне выгоднее: ИП или самозанятость?"})
    return sorted(events, key=lambda e: e["date"])


def _calendar(c: Client, ref: Refs, now: date) -> AccountAnswer:
    events = calendar_events(c, now)
    if not c.is_npd and not events:
        return _not_registered(c, ref, now, "calendar")
    if not events:
        return AccountAnswer("Ближайших сроков нет: чеки сформированы, налог за прошлый месяц уплачен или меньше 100 ₽ "
                             f"{ref('notice')}.", [])
    lines = ["Ваши ближайшие сроки:"]
    for e in events:
        mark = ref(e["rule"]) if e["rule"] else ""
        when = "сегодня" if e["date"] == now else ddmm(e["date"])
        lines.append(f"- **{when} — {e['title']}.** {e['text']} {mark}".rstrip())
    if c.is_npd and not c.autopay:
        lines.append(f"\nНалог можно не держать в голове: банк может платить его сам, если уполномочить его "
                     f"{ref('autopay')}. _Это предложение банка._")
    return AccountAnswer("\n".join(lines), [])


PRODUCTS = {
    "register": "Постановка на учёт как самозанятый через банк",
    "receipts": "Чеки через банк (банк — партнёр ФНС)",
    "autopay": "Автоплатёж налога",
    "ip": "ИП, расчётный счёт и эквайринг",
}


def leads(clients: dict, now: date | None = None) -> list[dict]:
    """Для банка: кому и что предложить — только по клиентам, давшим согласие на анализ поступлений."""
    now = now or today()
    out = []
    for c in clients.values():
        if not c.consent:
            continue
        lessons = [o for o in c.operations if o.work and (now - o.day).days <= 92]
        if not c.is_npd and lessons:
            out.append({"client": c.name, "stage": STAGES[stage(c, now)], "product": PRODUCTS["register"],
                        "signal": f"{plural(len(lessons), 'оплата', 'оплаты', 'оплат')} за работу за 3 месяца на "
                                  f"{money(sum(o.amount for o in lessons))}", "priority": 2})
            continue
        if not c.is_npd:
            continue
        missing = no_receipt(c, now)
        if missing:
            out.append({"client": c.name, "stage": STAGES[stage(c, now)], "product": PRODUCTS["receipts"],
                        "signal": f"{plural(len(missing), 'поступление', 'поступления', 'поступлений')} без чека "
                                  f"на {money(sum(o.amount for o in missing))}", "priority": 2})
        if not c.autopay:
            out.append({"client": c.name, "stage": STAGES[stage(c, now)], "product": PRODUCTS["autopay"],
                        "signal": "налог платит вручную", "priority": 3})
        total, _, _ = year_income(c, now)
        _, reach = limit_pace(c, now)
        x = crossing(c, now)
        if x is not None:
            out.append({"client": c.name, "stage": STAGES[stage(c, now)], "product": PRODUCTS["ip"],
                        "signal": f"лимит превышен {ddmm(x.day)} — 20 дней на переход в ИП", "priority": 1})
        elif total >= WARN_SHARE * LIMIT or (reach and (reach - now).days <= 90):
            signal = f"до лимита {money(max(LIMIT - total, 0))}"
            if reach:
                signal += f", при нынешнем темпе — около {ddmm(reach)}"
            out.append({"client": c.name, "stage": STAGES[stage(c, now)], "product": PRODUCTS["ip"], "signal": signal,
                        "priority": 1})
    return sorted(out, key=lambda x: x["priority"])


# ---------- уведомление в момент поступления ----------

def _op_tax(c: Client, key: str, now: date) -> tuple[float, float]:
    """Налог с одного поступления как с оплаты за работу: без бонуса и с бонусом (разница налога за месяц
    с этим поступлением и без него — бонус и перенос мелких сумм учтены так же, как в расчёте за месяц)."""
    o = next(x for x in c.operations if x.key == key)
    as_work = apply_overrides(c, {key: "work"})
    without = replace(c, operations=[x for x in c.operations if x.key != key])
    with_it = month_tax(as_work, o.day.year, o.day.month, now)
    base = month_tax(without, o.day.year, o.day.month, now)
    return round(with_it.tax - base.tax, 2), round(with_it.payable - base.payable, 2)


def notify(c: Client, key: str, chunks: list[Chunk], now: date | None = None) -> dict | None:
    """Уведомление в момент поступления: что это за перевод по закону и что сделать сейчас. Банк узнаёт
    о поступлении первым — поэтому может предложить чек, пока не прошёл срок «в момент расчёта», посчитать налог
    и предупредить о лимите в тот день, когда он превышен, а не после. c — клиент уже с этим поступлением.

    Действия: receipt — сформировать чек (для неясного перевода — и отметить его оплатой), work и personal —
    отметить тип поступления, check — проверка «подходит ли мне самозанятость», ask:<вопрос> — разобрать в чате."""
    now = now or today()
    o = next((x for x in c.operations if x.key == key), None)
    if o is None or o.kind == "salary":
        return None
    ref = Refs(chunks)
    head = f"Поступление {money(o.amount)} · {o.payer}"       # имя плательщика — в именительном падеже

    def notice(level: str, title: str, text: str, actions: list[str]) -> dict:
        return {"key": o.key, "level": level, "title": title, "text": text, "actions": actions,
                "sources": ref.sources, "deadline": receipt_deadline(o)}

    if not c.is_npd:
        if not o.work:
            return None
        return notice("info", head, f"Похоже на оплату за работу: «{o.purpose}». С такого дохода можно платить налог "
                                    f"как самозанятый — 4 % с оплаты от физлиц {ref('rates')}, а встать на учёт можно "
                                    f"прямо в приложении банка {ref('register_bank')}.", ["check"])
    if o.kind == "personal":
        return notice("info", head, f"По назначению «{o.purpose}» это личный перевод, а не доход самозанятого: налог "
                                    "и чек не нужны. Если это оплата за работу — отметьте, посчитаю налог и подготовлю "
                                    "чек.", ["work"])
    if o.key in {x.key for x, _ in former_employer_income(c)}:
        return notice("error", f"Оплата от бывшего работодателя · {money(o.amount)}",
                      f"{o.payer}, «{o.purpose}». С увольнения не прошло двух лет, поэтому такой доход не облагается "
                      f"налогом на профессиональный доход {ref('employer')}: оформить его как доход самозанятого "
                      "и сформировать чек нельзя.",
                      ["ask:Что с переводом от моего бывшего работодателя?"])
    x = crossing(c, now)
    if o.work and x is not None and any(y is o for y in over_limit(c, now)):
        first = o is x
        return notice("error", f"Лимит 2,4 млн ₽ превышен · {money(o.amount)}",
                      f"{o.payer}, «{o.purpose}». "
                      + (f"С этой оплатой доход с начала года — {money(gross_income(c, now))}, больше лимита "
                         f"{ref('limit')}. Право платить налог как самозанятый утрачено со дня превышения "
                         f"{ref('lose_right')}" if first else
                         f"Лимит превышен ещё {ddmm(x.day)} — право платить налог как самозанятый утрачено "
                         f"{ref('lose_right')}")
                      + f". Со всей суммы этой оплаты отчитываются по другой системе — чек не формируется "
                        f"{ref('over_limit')}; на переход на упрощёнку есть 20 календарных дней {ref('to_usn')}.",
                      ["ask:Что мне выгоднее: ИП или самозанятость?", "ask:Сколько мне осталось до лимита?"])
    plain, tax = _op_tax(c, key, now)
    rate = f"{round(RATE[o.payer_kind] * 100)} %"

    def tax_text() -> str:
        text = f"налог с неё — {money(plain)} ({rate} {ref('rates')})"
        return text + (f", а с бонусом — {money(tax)} {ref('deduction')}" if tax < plain - 0.005 else "")

    if o.kind == "unknown":
        return notice("warning", f"{head} — это оплата за работу?",
                      f"Из назначения «{o.purpose}» не понятно, оплата это или личный перевод. Если оплата — чек "
                      f"нужно сформировать {receipt_deadline(o)} {ref('receipt_term')}, {tax_text()}.",
                      ["receipt", "personal"])
    total, _, _ = year_income(c, now)
    text = (f"Оплата за работу: «{o.purpose}». Чек нужно сформировать {receipt_deadline(o)} {ref('receipt_term')}, "
            f"{tax_text()}.")
    level = "info"
    if total >= WARN_SHARE * LIMIT:
        level = "warning"
        text += f" До лимита 2,4 млн ₽ осталось {money(LIMIT - total)} {ref('limit')}."
    return notice(level, head, text, ["receipt", "personal"])


def summary(c: Client, now: date | None = None) -> str:
    """Короткая справка о клиенте для подсказки модели и журнала (без сумм по операциям)."""
    now = now or today()
    if not c.is_npd:
        return f"{c.name}, {c.age} лет, пока не самозанятый."
    total, _, _ = year_income(c, now)
    return f"{c.name}, {c.age} лет, самозанятый с {c.npd_since:%m.%Y}, доход с начала года {money(total)}."

