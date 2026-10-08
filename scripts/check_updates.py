"""Проверяет, не изменились ли законы и разъяснения ФНС с момента сборки базы знаний.

    python scripts/check_updates.py            # отчёт и data/kb_status.json (его показывает страница «Для банка»)
    python scripts/check_updates.py --apply    # заодно обновить тексты в data/raw/; затем scripts/build_index.py

Статьи сравниваются по тексту частей (без пробелов и регистра), разъяснения ФНС — по вопросам и ответам.
Устаревшие ответы ФНС, исключённые вручную (outdated в data/sources.yaml), продолжают исключаться и после
обновления. Запускать по расписанию (раз в неделю) — так база знаний не устаревает незаметно.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_laws  # noqa: E402

STATUS = ROOT / "data" / "kb_status.json"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("ё", "е")).strip().lower()


def split_articles(text: str) -> dict[str, str]:
    """«Статья 4. …» → {"4": текст статьи без заголовка и ссылки}."""
    out, current, lines = {}, None, []
    for line in text.splitlines():
        m = re.match(r"^Статья\s+(\d+(?:\.\d+)*)\.", line)
        if m:
            if current:
                out[current] = "\n".join(lines)
            current, lines = m[1], [line]
        elif current and not line.startswith(("@url", "#")):
            lines.append(line)
    if current:
        out[current] = "\n".join(lines)
    return out


def split_faq(text: str) -> dict[str, str]:
    out = {}
    for block in text.split("\nВопрос: ")[1:]:
        question, _, answer = block.partition("\n")
        out[_norm(question)] = answer
    return out


def diff(old: dict[str, str], new: dict[str, str], what: str) -> list[str]:
    changes = []
    for key in new:
        if key not in old:
            changes.append(f"новая {what}: {key[:90]}")
        elif _norm(old[key]) != _norm(new[key]):
            changes.append(f"изменена {what}: {key[:90]}")
    changes += [f"исключена {what}: {key[:90]}" for key in old if key not in new]
    return changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать новые тексты в data/raw/")
    args = ap.parse_args()
    docs = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))["documents"]
    report = {"checked": time.strftime("%Y-%m-%d %H:%M"), "documents": {}}
    for doc in docs:
        if doc.get("manual"):
            report["documents"][doc["id"]] = {"title": doc["short"], "status": "вручную",
                                             "changes": ["проверяется вручную (скан официальной публикации)"
                                                         if doc["structure"] == "law" else "пополняется банком"]}
            continue
        path = ROOT / "data" / "raw" / doc["file"]
        print(doc["short"], "…", flush=True)
        try:
            fresh = fetch_laws.fetch_law(doc) if doc["structure"] == "law" else fetch_laws.fetch_faq(doc)
        except Exception as e:  # noqa: BLE001 — сайт недоступен: отчёт об ошибке, а не падение
            report["documents"][doc["id"]] = {"title": doc["short"], "status": "ошибка", "changes": [str(e)[:200]]}
            continue
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if doc["structure"] == "law":
            changes = diff(split_articles(current), split_articles(fresh), "статья")
        else:
            changes = diff(split_faq(current), split_faq(fresh), "разъяснение")
        report["documents"][doc["id"]] = {"title": doc["short"], "status": "изменения" if changes else "актуально",
                                         "changes": changes[:50]}
        print(f"  {'изменений: ' + str(len(changes)) if changes else 'без изменений'}")
        for c in changes[:10]:
            print("   -", c)
        if changes and args.apply:
            path.write_text(fresh, encoding="utf-8")
            print(f"  → {path.relative_to(ROOT)} обновлён; пересоберите индекс: python scripts/build_index.py")
    STATUS.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Статус: {STATUS.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
