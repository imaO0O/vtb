"""Проверка «Подходит ли мне самозанятость?» — вход в воронку для тех, кто только начинает зарабатывать.

Восемь коротких вопросов, ответ — по правилам закона, без языковой модели: кто не вправе применять налог на
профессиональный доход (ч. 2 ст. 4 422-ФЗ), какие доходы им не облагаются (ч. 2 ст. 6), кто может встать на учёт
(ст. 5), что делать ИП на другом режиме (ч. 4 ст. 15). Каждый вывод — со ссылкой на пункт из базы знаний,
как в обычных ответах помощника. Итог: «подходит», «подходит с условиями» или «не подходит» — и следующий шаг.
"""
from dataclasses import dataclass, field

from .account import Refs
from .ingest import Chunk

# правило → (документ, статья, часть, пункт[, фраза])
RULES = {
    "citizens": ("fz422", "5", "3", None),
    "minors": ("faq_npd", None, None, None, "от четырнадцати до восемнадцати лет"),
    "excise": ("fz422", "4", "2", "1"),
    "resale": ("fz422", "4", "2", "2"),
    "mining": ("fz422", "4", "2", "3"),
    "employees": ("fz422", "4", "2", "4"),
    "agent": ("fz422", "4", "2", "5"),
    "delivery": ("fz422", "4", "2", "6"),
    "other_regime": ("fz422", "4", "2", "7"),
    "limit": ("fz422", "4", "2", "8"),
    "crypto": ("fz422", "4", "2", "9"),
    "rent_housing": ("fz422", "6", "2", "3"),
    "civil_servant": ("fz422", "6", "2", "4"),
    "employer": ("fz422", "6", "2", "8"),
    "refuse_usn": ("fz422", "15", "4", None),
    "register": ("fz422", "5", "4", None),
    "rates": ("fz422", "10", None, None),
    "deduction": ("fz422", "12", "1", None),
    "declaration": ("fz422", "13", None, None),
}

QUESTIONS = [
    {"id": "citizenship", "text": "Гражданство", "kind": "one",
     "options": [("rf", "Россия"), ("eaeu", "Страна ЕАЭС или Украина"), ("other", "Другая страна")]},
    {"id": "age", "text": "Сколько вам лет", "kind": "one",
     "options": [("18", "18 и больше"), ("14", "14–17"), ("13", "Меньше 14")]},
    {"id": "activity", "text": "Чем вы зарабатываете (можно несколько)", "kind": "many",
     "options": [("services", "Услуги и работы: фриланс, уроки, дизайн, ремонт"),
                 ("own_goods", "Продаю то, что делаю сам"),
                 ("rent_housing", "Сдаю квартиру или комнату"),
                 ("rent_other", "Сдаю нежилое помещение или апартаменты"),
                 ("resale", "Перепродаю купленные товары"),
                 ("excise", "Продаю подакцизные или маркированные товары"),
                 ("agent", "Продаю чужое по агентскому договору или договору комиссии"),
                 ("delivery", "Доставляю товары и принимаю за них оплату"),
                 ("mining", "Добываю или продаю полезные ископаемые"),
                 ("crypto", "Майнинг или продажа криптовалюты")]},
    {"id": "employees", "text": "Есть ли у вас работники по трудовому договору", "kind": "yesno"},
    {"id": "income", "text": "Доход за год может превысить 2,4 млн ₽", "kind": "yesno"},
    {"id": "regime", "text": "Вы ИП на другом налоговом режиме", "kind": "one",
     "options": [("none", "Нет"), ("special", "Да: УСН, патент или ЕСХН"), ("general", "Да: общий режим")]},
    {"id": "employer", "text": "Заказчик — ваш нынешний работодатель или бывший, с увольнения меньше двух лет",
     "kind": "yesno"},
    {"id": "civil", "text": "Вы государственный или муниципальный служащий", "kind": "yesno"},
]

BANNED = {"resale": ("resale", "Перепродажа товаров"), "excise": ("excise", "Подакцизные и маркированные товары"),
          "mining": ("mining", "Добыча и продажа полезных ископаемых"),
          "agent": ("agent", "Агентские договоры и договоры комиссии"),
          "crypto": ("crypto", "Майнинг и продажа криптовалюты")}


@dataclass
class Verdict:
    status: str                       # yes | conditions | no
    title: str
    items: list = field(default_factory=list)      # [(level, text)] — level: no | condition | ok
    next_steps: list = field(default_factory=list)
    sources: list = field(default_factory=list)


def check(answers: dict, chunks: list[Chunk]) -> Verdict:
    """answers: {citizenship, age, activity: [..], employees: bool, income: bool, regime, employer: bool, civil: bool}."""
    refs = Refs(chunks, RULES)
    ref = refs

    items = []
    if answers.get("citizenship") == "other":
        items.append(("no", f"Встать на учёт могут граждане России, стран ЕАЭС и Украины {ref('citizens')}."))
    if answers.get("age") == "13":
        items.append(("no", f"Самозанятым можно стать с 14 лет {ref('minors')}."))
    elif answers.get("age") == "14":
        items.append(("condition", "С 14 до 18 лет — при письменном согласии родителей (законных представителей), "
                                   f"после вступления в брак или эмансипации {ref('minors')}."))
    activity = set(answers.get("activity") or [])
    for key in sorted(activity & set(BANNED)):
        rule, name = BANNED[key]
        items.append(("no", f"{name}: применять налог на профессиональный доход нельзя {ref(rule)}."))
    if "delivery" in activity:
        items.append(("condition", "Доставка с приёмом оплаты — только если расчёты идут через кассу продавца "
                                   f"товаров {ref('delivery')}."))
    if "rent_other" in activity:
        items.append(("condition", "Сдача нежилого помещения и апартаментов: этот доход налогом самозанятого не "
                                   f"облагается — только аренда жилья {ref('rent_housing')}."))
    if answers.get("employees"):
        items.append(("no", f"С работниками по трудовому договору самозанятым быть нельзя {ref('employees')}."))
    if answers.get("income"):
        items.append(("no", f"Если доход за год превысит 2,4 млн ₽, право на режим теряется {ref('limit')} — "
                            "сразу подойдёт ИП на упрощёнке."))
    if answers.get("regime") == "special":
        items.append(("condition", "ИП на УСН, патенте или ЕСХН может перейти на налог на профессиональный доход, но в "
                                   "течение месяца после постановки на учёт должен уведомить налоговую об отказе от "
                                   f"прежнего режима {ref('refuse_usn')} — совмещать нельзя {ref('other_regime')}."))
    if answers.get("employer"):
        items.append(("condition", "Оплату от нынешнего работодателя или бывшего (меньше двух лет) нельзя учитывать "
                                   f"как доход самозанятого {ref('employer')}; от других заказчиков — можно."))
    if answers.get("civil"):
        items.append(("condition", "Госслужащим налог на профессиональный доход доступен только для сдачи жилья в "
                                   f"аренду {ref('civil_servant')}."))
        if activity - {"rent_housing"}:
            items.append(("no", "Для остальных ваших доходов как госслужащему режим не подходит "
                                f"{ref('civil_servant')}."))

    if any(level == "no" for level, _ in items):
        status, title = "no", "Самозанятость вам не подходит"
        steps = ["Посмотрите ИП на упрощёнке: спросите помощника «Какая ставка УСН и какие взносы платит ИП?»"]
    elif items:
        status, title = "conditions", "Подходит — с условиями"
        steps = []
    else:
        status, title = "yes", "Самозанятость вам подходит"
        steps = []
    if status != "no":
        items.append(("ok", f"Налог — 4 % с оплаты от людей и 6 % — от организаций и ИП {ref('rates')}; бонус "
                            f"10 000 ₽ снижает ставку {ref('deduction')}; декларацию сдавать не нужно "
                            f"{ref('declaration')}."))
        steps = [f"Встать на учёт — в приложении «Мой налог» или прямо в приложении банка {ref('register')}.",
                 "После этого помощник будет следить за чеками, налогом и лимитом по вашим поступлениям."] + steps
    return Verdict(status, title, items, steps, refs.sources)

