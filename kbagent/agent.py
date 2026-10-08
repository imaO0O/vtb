"""Агент: понимает вопрос в контексте диалога, выбирает источник (закон и разъяснения ФНС или счёт
клиента в банке), отвечает со ссылками и пишет журнал."""
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

import yaml

from . import account, llm
from .answer import answer_from_documents, source_label
from .cards import build_cards, card_chunk, card_text
from .config import settings
from .privacy import mask, prune
from .retriever import Hit, Retrieval, retrieve
from .store import Index
from .text import tokenize

CONDENSE_PROMPT = """Пользователь продолжает разговор с помощником по налогу для самозанятых.
Перепиши последний вопрос пользователя так, чтобы он был понятен без предыдущих реплик: подставь, о чём идёт речь.
Если вопрос и так понятен, верни его без изменений. Не отвечай на вопрос. Верни только переписанный вопрос одной строкой.

Предыдущие реплики:
{dialog}

Последний вопрос: {question}
Переписанный вопрос:"""

CLARIFY_PROMPT = """Вопрос пользователя слишком общий: «{question}».
В законе о самозанятых и разъяснениях ФНС по нему нашлись фрагменты на разные темы:
{topics}

Предложи 3 уточнённых вопроса, которые пользователь мог иметь в виду. Каждый вопрос сохраняет смысл исходного
и добавляет предмет из своей темы списка. Например, для вопроса «Какой срок?» — «До какого числа самозанятому
нужно заплатить налог?». По одному вопросу в строке, без нумерации и пояснений."""

SHORTEN_PROMPT = """Пользователь задал длинный вопрос помощнику по налогу для самозанятых.
Сформулируй суть одним коротким вопросом (до 20 слов): что именно он хочет узнать. Сохрани все условия,
суммы, сроки и виды деятельности; если вопросов несколько — сохрани все.
Приветствие и рассказ о себе убери. Не отвечай на вопрос. Верни только вопрос одной строкой.

Вопрос: {question}
Короткий вопрос:"""
LONG_QUESTION_WORDS = 25

# «Откуда это?», «Где это написано?», «А ты уверен?» — о предыдущем ответе: тот же вопрос, те же источники
SOURCE_Q_RE = re.compile(r"^\s*(?:а\s+|и\s+)?(?:откуда (?:это|ты (?:это )?(?:взял|знаешь)|информация|такие данные)|"
                         r"где (?:это )?(?:написано|сказано|указано)|(?:какой|каков) источник|на что (?:ты )?ссылаешься|"
                         r"ты уверен\w*|точно)\s*[?!.]*\s*$", re.I)
FOLLOWUP_RE = re.compile(
    r"^(?:а|и|но|тогда|ещё|еще)\b|\b(?:это|этого|этому|этом|этот|эта|эту|эти|этих|он|она|оно|они|его|её|ее|их|"
    r"него|неё|нее|ним|ней|нём|нем|там|туда|тот|та|те|того|такой|такое|такие|такая)\b", re.I)
# местоимение во второй части составного вопроса отсылает к первой — такую часть не отвечают отдельно
ANAPHORA_RE = re.compile(r"\b(?:них|ним|ними|их|него|нему|нём|неё|нее|ней|его|ему|им|её|ее|он|она|оно|они|это|"
                         r"этого|этому|этим|там|тогда)\b", re.I)
PART_SPLIT_RE = re.compile(
    r"\s*[,;]?\s+и\s+(?=(?:(?:к|в|на|о|об|по|с|у|от|для|из|при)\s+)?(?:как|что|кто|какой|какая|какое|какие|каким|"
    r"где|когда|сколько|куда|кому|кого|чем|почему|зачем|в течение|за какое|можно ли|нужно ли|надо ли)\b)|\?\s+(?=\S)",
    re.I)

# вопросы о составе базы знаний и о документе целиком
DOCS_Q_RE = re.compile(
    r"\b(?:какие|какой|какая|список|перечень|перечисли)\s+(?:(?:есть|имеются|у тебя|у вас|в базе)\s+)*"
    r"(?:документ\w*|закон\w*|нормативн\w*|источник\w*)\s*(?:есть|имеются|у тебя|в базе|ты\b|"
    r"используешь|[?.!]|$)|"
    r"в каких (?:документах|законах)|на (?:какие|каких) (?:документ|закон|источник)\w*|"
    r"(?:по каким|из каких) (?:документам|законам|источникам)", re.I)
# «какие документы нужны для ипотеки» — о документах для дела, а не о базе знаний помощника
DOCS_NEEDED_RE = re.compile(r"нужн\w*|требу\w*|предостав\w*|подать|собрать|оформ\w*", re.I)
SUMMARY_Q_RE = re.compile(
    r"\bо ч[её]м\b|кратк\w* содержани|кратко (?:опиши|перескажи|расскажи)|что за (?:документ|закон)|"
    r"что (?:содержит|описывает|устанавливает|регулирует)|для чего (?:нужен|предназначен)", re.I)

# Разговорные реплики и вопросы о самом помощнике: отвечаем справкой, а не поиском по закону
GREETING_RE = re.compile(r"^\s*(?:привет\w*|здравствуй\w*|добр\w+\s+(?:день|утро|вечер)|доброе утро|hello|hi|хай|"
                         r"салют|здорово)[\s!.,)]*$", re.I)
THANKS_RE = re.compile(r"^\s*(?:спасибо|благодарю|спс|понятно|ясно|ок|окей|хорошо|супер|класс)[\s!.,)]*(?:большое)?"
                       r"[\s!.,)]*$", re.I)
HELP_RE = re.compile(r"что (?:ты )?(?:умеешь|можешь)|кто ты|ты кто|как (?:тобой|с тобой) (?:пользоваться|работать)|"
                     r"^\s*(?:помощь|справка|help|меню)\s*\??\s*$", re.I)
ABOUT_RE = re.compile(r"откуда (?:ты )?(?:бер[её]шь|зна[её]шь|ответ|информаци|данные)|на ч[её]м (?:ты )?основ|"
                      r"как ты (?:работаешь|находишь|отвечаешь)|можно (?:ли )?тебе доверять|ты (?:не )?выдумываешь",
                      re.I)
MODEL_Q_RE = re.compile(r"chat\s*gpt|\bgpt\b|чат\s*гпт|\bгпт\b|gigachat|гигачат|кто тебя (?:сделал|создал|разработал)|"
                        r"(?:как\w*|на как\w*)\s+(?:ты\s+)?(?:модел\w*|нейросет\w*|ии\b|искусственн\w+)|"
                        r"\bты\s+(?:[—-]\s*)?(?:нейросеть|ии|бот|робот|человек)\b", re.I)
OFFLINE_Q_RE = re.compile(r"\b(?:ты|помощник\w*|работаешь|работает)\b[^?]{0,40}\bинтернет|"
                          r"нуж\w+ ли (?:тебе |для работы )?интернет|\bоблак|"
                          r"(?:переда[её]шь|отправляешь|отсылаешь)\b[^?]{0,30}(?:наружу|интернет|сервер|облак)", re.I)
ERRORS_Q_RE = re.compile(r"\b(?:ты|вы)\s+(?:можешь|может|можете)\s+(?:ли\s+)?ошиб|(?:можешь|бывает)\s+ли\s+(?:ты\s+)?ошиб|"
                         r"\bты\s+ошибаешься|насколько\s+(?:ты\s+)?точ\w+", re.I)
PRIVACY_Q_RE = re.compile(r"куда\s+уход\w*|кто\s+(?:видит|читает|смотрит)\s+(?:мои\s+)?(?:вопрос|ответ|запрос|диалог)|"
                          r"(?:сохраня|храни|храня|записыва)\w*\s+(?:ли\s+)?(?:ты\s+)?(?:мои\s+)?(?:вопрос|запрос|диалог)|"
                          r"конфиденциальн|\bмои\s+(?:вопросы|запросы)\b|видит ли (?:банк|налоговая|фнс)", re.I)
PROMPT_Q_RE = re.compile(r"системн\w*\s+(?:промпт|подсказк|инструкц)|(?:свой|свои|твой|твои)\s+(?:промпт|инструкци\w*|"
                         r"настройк\w*)|\bprompt\b|\bпромпт", re.I)
ACTION_Q_RE = re.compile(r"^\s*(?:сформируй|пробей|выбей|создай|отправь|переведи|оплати|заплати|зарегистрируй|"
                         r"удали|измени|отмени|аннулируй|оформи)\b(?:\s+(?:за\s+)?мне|\s+мой|\s+мою|\s+мои)?", re.I)
OPINION_Q_RE = re.compile(r"куда\s+(?:лучше\s+)?(?:вложить|инвестировать)|какие\s+акци|посоветуй\s+(?:банк|кредит|вклад|"
                          r"акци|криптовалют|инвестиц)|стоит\s+ли\s+(?:покупать|брать\s+кредит|инвестировать)|"
                          r"тво[её]\s+мнение|как\s+(?:ты\s+)?думаешь", re.I)
CREATIVE_Q_RE = re.compile(r"^\s*(?:напиши|сочини|придумай|расскажи)\s+(?:мне\s+)?(?:стих\w*|анекдот\w*|шутк\w*|"
                           r"сказк\w*|песн\w*|историю|рассказ)", re.I)
# Служебные вставки в вопросе: «игнорируй инструкции и…», «ответь без ссылок, своими словами» —
# вырезаются, остаётся сам вопрос (ответ всё равно только по закону)
META_RE = re.compile(
    r"(?:игнорируй|забудь|не\s+учитывай|отбрось)\s+(?:все\s+|свои\s+|предыдущие\s+|прошлые\s+)*"
    r"(?:инструкци\w*|правил\w*|указани\w*|ограничени\w*|документ\w*)[,;:.!]?\s*(?:и\s+)?|"
    r"(?:ответь|отвечай|скажи|напиши)\s+(?:мне\s+)?(?:без\s+(?:документов|ссылок|источников|закона)|своими\s+словами|"
    r"честно)[,;:.!—-]*\s*|без\s+(?:документов|ссылок|источников)[,;:—-]*\s*|своими\s+словами[,;:—-]*\s*|"
    r"(?:ответь|скажи|напиши|подтверди),?\s+что\s+", re.I)
# «Что делать?», «Сколько платить?» без предмета и без предыдущего вопроса — встречный вопрос
VAGUE_RE = re.compile(r"^\s*(?:а\s+|и\s+)?(?:что|как|куда|кому|где|сколько)\s+(?:мне\s+|нам\s+)?(?:делать|быть|поступить|"
                      r"звонить|обращаться|идти|смотреть|платить)\s*[?!.]*\s*$", re.I)

EXAMPLES = ["Какой лимит дохода у самозанятого?", "Можно ли работать как самозанятый на бывшего работодателя?",
            "Что будет, если не сформировать чек?", "Когда платить налог самозанятому?"]
ACCOUNT_EXAMPLES = ["Сколько мне осталось до лимита?", "Что мне выгоднее: ИП или самозанятость?",
                    "Сколько налога мне платить?"]
MODEL_TEXT = ("Я работаю на открытой отечественной языковой модели GigaChat 3.1 Lightning, которая установлена "
              "внутри контура банка. ChatGPT и другие внешние сервисы не используются. Поиск по закону тоже идёт "
              "внутри банка — моделями bge-m3 и bge-reranker-v2-m3.\n\nМодель не отвечает «от себя»: она получает "
              "только найденные статьи закона и разъяснения ФНС и составляет ответ по ним, со ссылкой на источник. "
              "Суммы по вашему счёту считает программа, а не модель.")
OFFLINE_TEXT = ("Интернет мне не нужен: языковая модель, поиск и база знаний работают внутри контура банка. "
                "Ваши вопросы и данные о счёте наружу не передаются.")
ERRORS_TEXT = ("Могу — поэтому к каждому ответу даю ссылку на статью закона или разъяснение ФНС: нажмите на источник "
               "и сверьте с текстом. Ответ без подтверждённой ссылки я не показываю, а если ответа в законе нет — так и "
               "говорю, не придумываю. Если ответ неверный, поставьте 👎 — банк разберёт его.")
PRIVACY_TEXT = ("Вопросы и ответы сохраняются в журнале банка, чтобы находить ошибки и темы, на которые нет ответа. "
                "Телефоны, почта, паспорт, СНИЛС, ИНН и ФИО в журнале маскируются. В ФНС и другие сервисы ничего "
                "не передаётся.")
PROMPT_TEXT = ("Служебные настройки я не показываю. Отвечаю только по закону о самозанятых, Налоговому кодексу и "
               "разъяснениям ФНС — задайте вопрос по ним, например:")
ACTION_TEXT = ("Я только отвечаю на вопросы и считаю по вашим поступлениям: сформировать чек, заплатить налог или "
               "изменить данные не могу. Чеки и налог — в приложении «Мой налог», переводы — в приложении банка. "
               "Могу подсказать, например:")
OPINION_TEXT = ("Инвестиционных и финансовых советов «от себя» я не даю — отвечаю только тем, что написано в законе "
                "о самозанятых и разъяснениях ФНС. Спросите, например:")
CREATIVE_TEXT = "Стихов и анекдотов не пишу — я помощник по налогу для самозанятых. Спросите, например:"
ABOUT_TEXT = ("Я отвечаю только по закону № 422-ФЗ о налоге на профессиональный доход, статьям Налогового кодекса "
              "и разъяснениям ФНС — без интернета и без собственных знаний модели.\n\n"
              "1. По вопросу ищу подходящие статьи и разъяснения — по словам и по смыслу — и оцениваю, отвечают ли они "
              "на вопрос.\n"
              "2. Ответ составляю только из найденного; каждое утверждение — со ссылкой [n] на статью, часть и пункт. "
              "Нажмите на источник, чтобы прочитать текст закона.\n"
              "3. Ответ без подтверждённой ссылки не показываю. Если ответа в законе нет — так и говорю.\n"
              "4. Вопросы о ваших деньгах («сколько мне осталось до лимита») считаю по поступлениям на счёт в банке — "
              "это делает программа, а правила к расчёту — снова со ссылкой на закон.")

# Как вопрос называет документ → id документа
DOC_NAMES = [
    (re.compile(r"422\s*-?\s*фз|закон\w*\s+(?:о|об|про)\s+(?:самозанят|нпд|налог\w*\s+на\s+проф|эксперимент)", re.I), "fz422"),
    (re.compile(r"\bнк\b|\bнк\s*рф|налогов\w+\s+кодекс", re.I), "nk_rf"),
    (re.compile(r"\bфнс\b|налогов\w+\s+служб|разъяснени", re.I), "faq_npd"),
]
ARTICLE_RE = re.compile(r"(?:\bстать\w*|\bст\.)\s*(\d{1,3}(?:\.\d{1,2}){0,2})", re.I)


def named_docs(question: str) -> set[str]:
    return {doc for rx, doc in DOC_NAMES if rx.search(question)}


def merge(base, extra, limit: int) -> None:
    """Добавляет к найденному до `limit` новых уверенных фрагментов из другого поиска."""
    seen = {h.chunk.id: h for h in base.hits}
    for h in extra.hits:
        old = seen.get(h.chunk.id)
        if old is not None and not h.neighbor and h.score > old.score:
            old.score, old.neighbor = h.score, False
    new = [h for h in extra.hits if not h.neighbor and h.chunk.id not in seen
           and h.score >= settings.rerank_threshold][:limit]
    base.hits += new
    if extra.best_score > base.best_score:
        base.best_score, base.confident = extra.best_score, extra.confident


def split_parts(question: str) -> list[str]:
    """«Какой лимит дохода и когда платить налог?» → две части."""
    if re.search(r"[.!]\s+[А-ЯЁA-Z]", question.strip().rstrip("?")):
        return [question]          # вводное предложение нужно обеим частям
    parts = [p.strip(" ,;?") for p in PART_SPLIT_RE.split(question)]
    parts = [p for p in parts if len(tokenize(p)) >= 2]
    return parts if len(parts) > 1 else [question]


def _load_glossary() -> dict[str, list[str]]:
    path = settings.raw_dir.parent / "glossary.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("terms", {}) or {}


GLOSSARY = _load_glossary()
TOPIC_NOISE_RE = re.compile(r"\b(?:в базе знаний|базе знаний|база знаний|у тебя|вообще|всего|доступн\w*|ты знаешь|"
                            r"знаешь|есть|имеются|у вас|в базе|про|по теме|на тему|о|об|по|касаются|касающиеся|"
                            r"описан\w*|говорится|упоминается|написано|ты|используешь|опираешься|отвечаешь)\b", re.I)


RU_WORD_RE = re.compile(r"[а-яё]{4,}", re.I)
RU_LETTERS = "абвгдежзийклмнопрстуфхцчшщъыьэюя"


def vocabulary(chunks) -> Counter:
    """Слова корпуса с частотой — для исправления опечаток в вопросе."""
    return Counter(w.lower().replace("ё", "е") for c in chunks for w in RU_WORD_RE.findall(c.search_text))


def _edits(word: str) -> set[str]:
    """Все слова на одно редактирование от word: пропуск, перестановка, замена и вставка буквы."""
    splits = [(word[:i], word[i:]) for i in range(len(word) + 1)]
    return ({a + b[1:] for a, b in splits if b} | {a + b[1] + b[0] + b[2:] for a, b in splits if len(b) > 1}
            | {a + ch + b[1:] for a, b in splits if b for ch in RU_LETTERS} | {a + ch + b for a, b in splits
                                                                           for ch in RU_LETTERS})


def typo_fixes(question: str, vocab: Counter) -> list[str]:
    """«до какого чсла … в нолог» → [«числа», «налог»]: слово, которого нет в корпусе ни в какой форме,
    заменяется самым частым словом корпуса на одно редактирование от него."""
    if not vocab:
        return []
    stems = {tokenize(w)[0] for w in vocab if tokenize(w)}
    slang = {s for term in GLOSSARY for s in tokenize(term)}      # «скока» — в словаре, а не опечатка
    out = []
    for word in RU_WORD_RE.findall(question.lower().replace("ё", "е")):
        if word in vocab or (tokenize(word) and tokenize(word)[0] in stems | slang):
            continue
        near = [w for w in _edits(word) if w in vocab]
        if near:
            out.append(max(near, key=vocab.get))
    return out


def expand(question: str, vocab: Counter | None = None) -> str:
    """Разговорные слова → как это называется в законе: «бонус» → «налоговый вычет», «самозанятый» →
    «налог на профессиональный доход»; слова с опечаткой → правильное слово из корпуса. Нужно и
    переранжировщику: он не знает, что самозанятый — это плательщик НПД, и без подсказки оценивает пункт закона
    о «налогоплательщиках» в 0,005 вместо 0,64. Модель видит исходный вопрос."""
    fixes = typo_fixes(question, vocab) if vocab else []
    words = set(tokenize(question)) | {s for w in fixes for s in tokenize(w)}
    extra = fixes + [w for term, synonyms in GLOSSARY.items() if set(tokenize(term)) <= words for w in synonyms]
    extra = [w for w in dict.fromkeys(extra) if not set(tokenize(w)) <= set(tokenize(question))]
    return f"{question} ({', '.join(extra)})" if extra else question


@dataclass
class AgentResult:
    question: str
    role: str
    route: str                     # documents | account | help
    status: str                    # answer | no_data | model_refused | unverified | clarify | info | error
    text: str
    sources: list = field(default_factory=list)   # [{n, label, text, cited, score, url, ...}]
    columns: list = field(default_factory=list)   # таблица операций по счёту
    rows: list = field(default_factory=list)
    seconds: float = 0.0
    stats: dict = field(default_factory=dict)
    asked: str | None = None       # вопрос, переписанный с учётом диалога (если отличается)
    options: list = field(default_factory=list)   # варианты уточнения для status == clarify / примеры для справки

    @property
    def answered(self) -> bool:
        return self.status == "answer"


BASIS_RE = re.compile(r"(?:(?:п\.|пункт\w*)\s*(\d{1,2})\s+)?(?:(?:ч\.|част\w*)\s*(\d{1,2}(?:\.\d)?(?:\s*(?:,|и)\s*\d{1,2})*)\.?\s*,?\s*)?"
                      r"(?:ст\.|стать\w*)\s*(\d{1,3}(?:\.\d{1,2})?)", re.I)
BASIS_DOCS = [(re.compile(r"422-ФЗ|27\.11\.2018|27 ноября 2018"), "fz422"),
              (re.compile(r"456-ФЗ|15\.12\.2025"), "fz456"),
              (re.compile(r"Налогов\w+\s+кодекс|НК\s+РФ"), "nk_rf"),
              (re.compile(r"Федеральн\w+\s+закон|кодекс", re.I), None)]   # другой закон — в базе знаний его нет


def law_basis(text: str, chunks: list) -> list[dict]:
    """Статьи закона, на которые ссылается разъяснение ФНС («Основание, указанное ФНС: ч. 2 ст. 4 …422-ФЗ»), —
    фрагменты из базы знаний, чтобы под ответом было видно и разъяснение, и норму закона."""
    line = next((ln for ln in text.splitlines() if ln.startswith("Основание, указанное ФНС")), "")
    finder, out, seen = account.Refs(chunks), [], set()
    for m in BASIS_RE.finditer(line):
        tail = line[m.end():m.end() + 160]
        doc = next((d for rx, d in BASIS_DOCS if rx.search(tail)), None)
        if doc is None:
            continue
        parts = re.findall(r"\d{1,2}(?:\.\d)?", m[2]) if m[2] else [None]
        for part in parts:
            chunk = finder.find(doc, m[3], part, m[1] if part else None)
            if chunk is not None and chunk.id not in seen:
                seen.add(chunk.id)
                out.append({"label": f"{chunk.short}, {chunk.ref}", "text": chunk.text, "url": chunk.url})
    return out[:3]


def _sources(a, chunks: list | None = None) -> list[dict]:
    out = []
    for n, h in enumerate(a.hits, 1):
        c = h.chunk
        source = {"n": n, "label": source_label(h), "text": c.text, "doc_id": c.doc_id, "doc_kind": c.doc_kind,
                  "clause": c.clause, "article": c.article, "item": c.item, "url": c.url, "page": c.page_start,
                  "page_end": c.page_end, "cited": n in a.cited, "score": round(h.score, 3)}
        if chunks and n in a.cited and c.doc_kind.startswith("Разъяснения"):
            source["basis"] = law_basis(c.text, chunks)
        out.append(source)
    return out


class Agent:
    def __init__(self, index: Index | None = None):
        self.index = index or Index.load(settings.index_dir)
        self._cards, self._cards_key = {}, None
        self.vocab = vocabulary(self.index.chunks)
        prune(settings.log_dir / "audit.jsonl")       # журнал вопросов — не дольше срока хранения

    # ---------- документы целиком ----------

    @property
    def cards(self) -> dict[str, dict]:
        key = (len(self.index.chunks), self.index.chunks[-1].id if self.index.chunks else None)
        if key != self._cards_key:
            self._cards, self._cards_key = build_cards(self.index), key
        return self._cards

    def _ranked_cards(self, query: str, allowed: set[str]) -> list[tuple[float, dict]]:
        cards = [c for c in self.cards.values() if c["access"] in allowed]
        if not cards:
            return []
        scores = llm.rerank(query, [card_text(c)[:1500] for c in cards])
        return sorted(zip(scores, cards), key=lambda x: -x[0])

    def answer_catalog(self, question: str, role: str, allowed: set[str]) -> "AgentResult":
        """«Какие документы у тебя есть?» — список из каталога, без генерации текста."""
        topic = re.sub(r"[?!.,]+", " ", TOPIC_NOISE_RE.sub(" ", DOCS_Q_RE.sub(" ", question))).strip()
        cards = sorted((c for c in self.cards.values() if c["access"] in allowed), key=lambda c: c["kind"])
        if tokenize(topic):
            have = {h.chunk.doc_id for h in retrieve(self.index, expand(topic, self.vocab), allowed).hits
                    if not h.neighbor and h.score >= 0.1}
            chosen = [c for c in cards if c["doc_id"] in have]
            head = f"Документы по теме «{topic}»:" if chosen else f"Документов по теме «{topic}» в базе знаний нет."
        else:
            chosen, head = cards, "Я отвечаю по этим документам:"
        lines = [f"{n}. {c['title']}" + (f" ({c['edition']})" if c.get("edition") else "") + f" [{n}]"
                 for n, c in enumerate(chosen, 1)]
        sources = [{"n": n, "label": c["short"], "text": card_text(c, sections=60), "doc_id": c["doc_id"],
                    "doc_kind": c["kind"], "clause": None, "article": None, "item": None, "url": c.get("url"),
                    "page": None, "page_end": None, "cited": True, "score": 1.0}
                   for n, c in enumerate(chosen, 1)]
        return AgentResult(question, role, "documents", "answer" if chosen else "no_data", "\n".join([head] + lines),
                           sources=sources, stats={"catalog": True})

    def answer_summary(self, question: str, role: str, allowed: set[str]) -> "AgentResult | None":
        """«О чём закон 422-ФЗ?» — пересказ по карточке документа (начало и перечень статей)."""
        named = named_docs(question)
        if named:
            ranked = [(1.0, self.cards[d]) for d in named if d in self.cards and self.cards[d]["access"] in allowed]
        else:
            ranked = self._ranked_cards(question, allowed)
        if not ranked or ranked[0][0] < 0.3:
            return None            # документ не назван явно — обычный поиск по фрагментам
        score, card = ranked[0]
        retrieval = Retrieval(hits=[Hit(card_chunk(card), score, None, None)], best_score=score,
                              confident=True, scored_by="reranker")
        a = answer_from_documents(question, retrieval)
        return AgentResult(question, role, "documents", a.status, a.text, sources=_sources(a),
                           stats={**a.stats, "summary": True})

    def article_retrieval(self, question: str, docs: set[str], allowed: set[str]) -> Retrieval | None:
        """«Что сказано в статье 14 422-ФЗ?» — фрагменты именно этой статьи, лучшие по переранжировщику."""
        m = ARTICLE_RE.search(question)
        if not m:
            return None
        number = m[1]
        chunks = [c for c in self.index.chunks if c.article == number and c.access in allowed
                  and (not docs or c.doc_id in docs)]
        if len({c.doc_id for c in chunks}) > 1:
            chunks = [c for c in chunks if c.doc_id == "fz422"] or chunks    # без названия — статья закона о НПД
        if not chunks:
            return None
        try:
            scores = llm.rerank(question, [c.search_text for c in chunks])
        except llm.ModelError:
            scores = [0.5] * len(chunks)
        ranked = sorted(zip(scores, chunks), key=lambda x: -x[0])[:settings.context_chunks]
        hits = [Hit(c, s, None, None) for s, c in ranked]
        # статью назвали прямо — отвечаем по ней, даже если вопрос общий («что в статье 13?»)
        return Retrieval(hits=hits, best_score=max(hits[0].score, settings.confident_threshold), confident=True,
                         scored_by="reranker")

    # ---------- понимание вопроса ----------

    @staticmethod
    def needs_context(question: str) -> bool:
        """«А если ИП?», «Почему?», «А это сколько?» — уточнение. «Может ли самозанятый получать больничный?» —
        самостоятельный вопрос, хотя в нём всего три значимых слова."""
        return len(tokenize(question)) <= 2 or bool(FOLLOWUP_RE.search(question))

    def condense(self, question: str, history: list[dict]) -> str:
        """«А если от организации?» после вопроса о ставке → «Какая ставка налога, если платит организация?»"""
        dialog = "\n".join(f"Пользователь: {h['question']}\nПомощник: {h.get('text', '')[:300]}" for h in history[-3:])
        try:
            out, _ = llm.chat([{"role": "user", "content": CONDENSE_PROMPT.format(dialog=dialog, question=question)}],
                              max_tokens=80)
        except llm.ModelError:
            return question
        out = out.strip().splitlines()[0].strip(" «»\"") if out.strip() else ""
        unchanged = out.lower().rstrip("?. ") == question.lower().rstrip("?. ")
        if unchanged and not FOLLOWUP_RE.search(question):
            return question           # модель сочла вопрос самостоятельным — не приклеиваем к нему прошлый
        if (not 5 <= len(out) <= 300 or unchanged
                or re.search(r"\b(?:это|этом|этого|он|она|оно|они)\b", out, re.I)
                or re.match(r"^\s*(?:а|и)\s", out, re.I)):
            # модель не переписала уточнение или оставила «это» — ищем по предыдущему вопросу вместе с ним:
            # «Какой штраф, если не передать чек? А если это повторилось через месяц?»
            return f"{history[-1]['question'].rstrip()} {question}"
        return out

    def clarify(self, question: str, retrieval) -> list[str]:
        words, topics, seen = set(tokenize(question)), [], set()
        for h in retrieval.hits:
            key = (h.chunk.doc_id, h.chunk.article, h.chunk.heading)
            if h.neighbor or key in seen:
                continue
            seen.add(key)
            sentences = re.split(r"(?<=[.;!?])\s+", h.chunk.text)
            hit = next((s for s in sentences if words & set(tokenize(s))), sentences[0])
            topics.append(f"- {h.chunk.heading or h.chunk.doc_title}: «{hit.strip()[:160]}»")
        try:
            out, _ = llm.chat([{"role": "user", "content": CLARIFY_PROMPT.format(
                question=question, topics="\n".join(topics[:5]))}], max_tokens=150)
        except llm.ModelError:
            return []
        options = [re.sub(r"^[\s\d.)•*-]+", "", line).strip() for line in out.splitlines()]
        return [o for o in options if len(o) > 8][:3]

    def shorten(self, question: str) -> str:
        """Длинный вопрос с приветствием и рассказом о себе — к сути: иначе поиск тонет в лишних словах."""
        try:
            out, _ = llm.chat([{"role": "user", "content": SHORTEN_PROMPT.format(question=question)}], max_tokens=80)
        except llm.ModelError:
            return question
        out = out.strip().splitlines()[0].strip(" «»\"") if out.strip() else ""
        numbers = set(re.findall(r"\d+", question))
        if not 8 <= len(out) <= 250 or numbers - set(re.findall(r"\d+", out)):
            return question          # сжатие потеряло сумму или срок — ищем по исходному вопросу
        return out

    @staticmethod
    def is_vague(question: str, retrieval) -> bool:
        """Короткий вопрос без предмета, а найденное разбросано по разным статьям."""
        topics = {(h.chunk.doc_id, h.chunk.article or h.chunk.heading) for h in retrieval.hits if not h.neighbor}
        return (len(tokenize(question)) <= 2 and len(topics) >= 3
                and settings.rerank_threshold <= retrieval.best_score < settings.confident_threshold)

    # ---------- справка ----------

    def small_talk(self, question: str, role: str, client: "account.Client | None") -> "AgentResult | None":
        examples = list(EXAMPLES[:2] + (ACCOUNT_EXAMPLES[:2] if client and client.is_npd else EXAMPLES[2:]))
        faq = [(PROMPT_Q_RE, PROMPT_TEXT, EXAMPLES), (ACTION_Q_RE, ACTION_TEXT, examples),
               (CREATIVE_Q_RE, CREATIVE_TEXT, EXAMPLES), (MODEL_Q_RE, MODEL_TEXT, None),
               (OFFLINE_Q_RE, OFFLINE_TEXT, None), (ERRORS_Q_RE, ERRORS_TEXT, None),
               (PRIVACY_Q_RE, PRIVACY_TEXT, None), (OPINION_Q_RE, OPINION_TEXT, EXAMPLES)]
        for rx, text, options in faq:
            if rx.search(question):
                return AgentResult(question, role, "help", "info", text, options=list(options or []))
        if GREETING_RE.match(question):
            text = ("Здравствуйте! Я помогаю разобраться с налогом для самозанятых: отвечаю по закону № 422-ФЗ, "
                    "Налоговому кодексу и разъяснениям ФНС — со ссылкой на статью. Если ответа в законе нет, так и "
                    "скажу." + (" А ещё вижу ваши поступления и подскажу про лимит, чеки и налог." if client else "")
                    + " Например:")
        elif HELP_RE.search(question):
            text = ("Я отвечаю на вопросы о налоге на профессиональный доход (НПД) простыми словами:\n\n"
                    "- кто может стать самозанятым и с какими доходами нельзя;\n"
                    "- ставки 4 % и 6 %, бонус 10 000 ₽, когда и как платить налог;\n"
                    "- чеки: когда формировать и какой штраф, если не сформировать;\n"
                    "- лимит 2,4 млн ₽ в год и переход в ИП;\n"
                    + ("- ваши поступления: сколько осталось до лимита, где нет чека, сколько налога за месяц.\n"
                       if client else "")
                    + "\nКаждый ответ — со ссылкой на статью закона или разъяснение ФНС. Например:")
        elif ABOUT_RE.search(question):
            return AgentResult(question, role, "help", "info", ABOUT_TEXT)
        elif THANKS_RE.match(question):
            return AgentResult(question, role, "help", "info", "Пожалуйста! Если появятся вопросы — спрашивайте.")
        elif not tokenize(question):
            text = "Не понял вопрос. Спросите, например:"
        else:
            return None
        return AgentResult(question, role, "help", "info", text, options=examples)

    # ---------- ответ ----------

    def ask(self, question: str, role: str = "Клиент", client: "account.Client | None" = None,
            history: list[dict] | None = None, on_step: Callable[[str], None] | None = None,
            user: str | None = None, log: bool = True, route: str = "auto") -> AgentResult:
        """route: auto — сам выбирает между законом и счётом клиента; documents — только закон."""
        t0 = time.time()
        step = on_step or (lambda _: None)
        allowed = settings.roles.get(role, {"all"})

        base = re.sub(r"^[\s,;:.!—-]+", "", META_RE.sub("", question)).strip() or question
        if base != question:
            base = base[:1].upper() + base[1:]
        talk = self.small_talk(base, role, client)
        if talk is not None:
            return self._done(talk, question, base, t0, user, log, client)

        asked = base
        if history and SOURCE_Q_RE.match(base):
            asked = history[-1]["question"]        # «Откуда это?» — тот же вопрос, те же источники
        elif history and self.needs_context(base):
            step("Уточняю вопрос по ходу диалога…")
            asked = self.condense(base, history)
        elif len(base.split()) > LONG_QUESTION_WORDS:
            step("Выделяю суть вопроса…")
            asked = self.shorten(base)

        # вопрос о собственных деньгах — считаем по счёту, правила — со ссылками на закон
        intent = account.detect_intent(asked) if client is not None and route == "auto" else None
        if intent:
            step("Смотрю поступления на счёте…")
            a = account.answer(intent, client, self.index.chunks, question=asked)
            result = AgentResult(question, role, "account", "answer", a.text, sources=a.sources, columns=a.columns,
                                 rows=a.rows, stats={"intent": intent})
            return self._done(result, question, asked, t0, user, log, client)

        vague = bool(VAGUE_RE.match(asked)) or (len(asked.split()) == 1 and not ARTICLE_RE.search(asked))
        result = None
        if DOCS_Q_RE.search(asked) and not DOCS_NEEDED_RE.search(asked):
            step("Смотрю, какие документы есть в базе знаний…")
            result = self.answer_catalog(asked, role, allowed)
        elif SUMMARY_Q_RE.search(asked) and not ARTICLE_RE.search(asked):
            step("Ищу документ и его описание…")
            result = self.answer_summary(asked, role, allowed)
        if result is not None:
            return self._done(result, question, asked, t0, user, log, client)

        step("Ищу в законе и разъяснениях ФНС…")
        parts = split_parts(asked)
        docs = named_docs(asked)
        retrieval = self.article_retrieval(asked, docs, allowed)
        if retrieval is None:
            query = expand(asked, self.vocab)
            retrieval = retrieve(self.index, query, allowed, docs or None)
            if docs and not retrieval.confident:
                retrieval = retrieve(self.index, query, allowed)
            if asked != base and history:
                # уточнение в диалоге: ищем и вместе с темой предыдущего вопроса
                extra = retrieve(self.index, f"{asked} {history[-1]['question']}", allowed)
                seen = {h.chunk.id for h in retrieval.hits}
                retrieval.hits += [h for h in extra.hits if not h.neighbor and h.chunk.id not in seen][:2]
            if len(parts) > 1 and not any(ANAPHORA_RE.search(p) for p in parts[1:]):
                # составной вопрос — ищем по каждой части, иначе находятся статьи только про одну из них
                for part in parts:
                    merge(retrieval, retrieve(self.index, expand(part, self.vocab), allowed), limit=2)
            prev_docs = set((history or [{}])[-1].get("docs") or [])
            if (asked != base and history and not retrieval.confident and retrieval.hits
                    and retrieval.hits[0].chunk.doc_id in prev_docs
                    and retrieval.best_score >= settings.rerank_threshold / 3):
                retrieval.confident = True        # уточнение к прошлому ответу — с проверкой «зоны сомнения»
            if vague or self.is_vague(asked, retrieval):
                step("Вопрос слишком общий — подбираю уточнения…")
                options = self.clarify(asked, retrieval)
                if options:
                    result = AgentResult(question, role, "documents", "clarify",
                                         "Вопрос слишком общий — уточните, что именно вас интересует:",
                                         options=options, stats={"best_score": round(retrieval.best_score, 3)})
                    return self._done(result, question, asked, t0, user, log, client)
        step(f"Найдено фрагментов: {len(retrieval.hits)}. Составляю ответ и проверяю ссылки…"
             if retrieval.confident else "Подходящих статей не нашлось")
        a = answer_from_documents(asked, retrieval, parts=parts)
        result = AgentResult(question, role, "documents", a.status, a.text, sources=_sources(a, self.index.chunks),
                             stats=a.stats)
        if not a.answered and client is not None and client.is_npd and account.FIRST_PERSON_RE.search(asked):
            result.options = ACCOUNT_EXAMPLES[:2]      # вопрос о себе, а ответа в законе нет — подсказываем расчёты
        return self._done(result, question, asked, t0, user, log, client)

    def _done(self, result: AgentResult, question: str, asked: str, t0: float, user: str | None, log: bool,
              client) -> AgentResult:
        result.question = question
        result.asked = asked if asked != question else None
        result.seconds = round(time.time() - t0, 2)
        if log:
            self._log(result, user, client.id if client else None)
        return result

    @staticmethod
    def _log(r: AgentResult, user: str | None, client: str | None = None) -> None:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "user": user, "client": client, "role": r.role,
                  "question": mask(r.question), "asked": mask(r.asked), "route": r.route, "status": r.status,
                  "answer": mask(r.text), "seconds": r.seconds,
                  "cited": [s["label"] for s in r.sources if s["cited"]], "intent": r.stats.get("intent")}
        with open(settings.log_dir / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
