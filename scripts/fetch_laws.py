"""Скачивает тексты базы знаний и сохраняет их в data/raw/ в простом текстовом виде.

    python scripts/fetch_laws.py              # все документы из data/sources.yaml
    python scripts/fetch_laws.py --only fz422

Законы — по статьям с consultant.ru (действующая редакция): строка «Статья N. …», затем части
«1. …» и пункты «1) …» — номера сохраняются, по ним строится ссылка в ответе. Примечания сайта
(«в ред. …», «см. предыдущую редакцию») не сохраняются. Разъяснения ФНС — вопросы и ответы
с npd.nalog.ru/faq: «Вопрос: …», текст ответа, «Источник: …» (статья закона, на которую ссылается ФНС).
"""
import argparse
import html
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36"}
SKIP_CLASSES = ("document__insert", "document__edit")       # примечания КонсультантПлюс и пометки о редакциях


class _Article(HTMLParser):
    """Абзацы текста статьи: всё внутри document-page__content, кроме примечаний сайта."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0          # вложенность div внутри содержимого статьи (0 — вне его)
        self.skip_at = None     # глубина div-примечания, внутри которого текст пропускается
        self.paragraphs, self._buf, self._in_p = [], [], False

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class") or ""
        if tag == "div":
            if self.depth:
                self.depth += 1
                if self.skip_at is None and any(c in cls for c in SKIP_CLASSES):
                    self.skip_at = self.depth
            elif "document-page__content" in cls:
                self.depth = 1
        elif tag == "p" and self.depth and self.skip_at is None:
            self._in_p, self._buf = True, []
        elif tag == "br" and self._in_p:
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if tag == "p" and self._in_p:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if text:
                self.paragraphs.append(text)
            self._in_p = False
        elif tag == "div" and self.depth:
            if self.skip_at == self.depth:
                self.skip_at = None
            self.depth -= 1

    def handle_data(self, data):
        if self._in_p and self.skip_at is None:
            self._buf.append(data)


def get(url: str) -> str:
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=60)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
                return r.text
        except requests.RequestException:
            pass
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Не удалось скачать {url}")


def article_url(doc: dict, number: str, ref: str) -> str:
    return ref if ref.startswith("http") else f"{doc['url'].rstrip('/')}/{ref}/"


def fetch_law(doc: dict) -> str:
    lines = [f"# {doc['title']}", f"# Текст получен {time.strftime('%d.%m.%Y')} с {doc['url']} ({doc['edition']})", ""]
    for number, ref in doc["articles"].items():
        url = article_url(doc, number, ref)
        parser = _Article()
        parser.feed(get(url))
        paragraphs = parser.paragraphs
        # «НК РФ Статья 129.13. …» — в кодексе перед номером статьи стоит название кодекса
        at = next((k for k, p in enumerate(paragraphs) if re.match(rf"^(?:НК РФ\s+)?Статья {re.escape(number)}\.", p)),
                  None)
        if at is None:
            raise RuntimeError(f"{doc['id']}: на странице {url} нет заголовка статьи {number}")
        title = re.sub(r"^НК РФ\s+", "", paragraphs[at])
        body = paragraphs[at + 1:]
        lines += [title, f"@url {url}"] + body + [""]
        print(f"  {title[:90]}: абзацев {len(body)}")
        time.sleep(1)
    return "\n".join(lines)


FAQ_RE = re.compile(r'<div class="togglet">(.*?)</div>\s*<div class="togglec"[^>]*>(.*?)</div>', re.S)


def _plain(fragment: str) -> list[str]:
    text = re.sub(r"<br\s*/?>|</p>|</li>|<li[^>]*>", "\n", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return [re.sub(r"\s+", " ", line).strip(" \t•") for line in text.split("\n") if line.strip(" \t•")]


def fetch_faq(doc: dict) -> str:
    page = get(doc["url"])
    lines = [f"# {doc['title']}", f"# Текст получен {time.strftime('%d.%m.%Y')} с {doc['url']}", ""]
    count = 0
    for q_html, a_html in FAQ_RE.findall(page):
        question = " ".join(_plain(q_html))
        answer, _, source = a_html.partition("<h4>Источник:</h4>")
        body = _plain(answer)
        if not question or not body:
            continue
        lines.append(f"Вопрос: {question}")
        lines += body
        if _plain(source):
            lines.append("Источник: " + " ".join(_plain(source)))
        lines.append("")
        count += 1
    print(f"  вопросов и ответов: {count}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="id документа из data/sources.yaml")
    args = ap.parse_args()
    docs = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))["documents"]
    for doc in docs:
        if args.only and doc["id"] != args.only:
            continue
        if doc.get("manual"):
            print(f"{doc['short']}: текст подготовлен вручную ({doc['file']}), пропускаю")
            continue
        print(doc["short"])
        text = fetch_law(doc) if doc["structure"] == "law" else fetch_faq(doc)
        target = ROOT / "data" / "raw" / doc["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"  → {target.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
