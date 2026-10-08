"""Прогон эталонного набора вопросов и подсчёт метрик.

    python eval/run_eval.py                                   # eval/questions.yaml → results/latest.*
    python eval/run_eval.py --questions eval/blind.yaml --out blind
    python eval/run_eval.py --only D01,D02 --out debug

Ответ засчитывается как верный, только если (1) в тексте ответа есть все ключевые факты эталона и
(2) среди процитированных источников есть верный: нужная статья и часть закона либо разъяснение ФНС,
в тексте которого эти факты действительно есть. Вопросы задаются так же, как в чате: от имени демо-клиента
(по умолчанию — Ани), чтобы помощник мог ошибиться и с выбором между законом и счётом.

Результаты: eval/results/<out>.md (таблица для заявки и презентации), <out>.csv, <out>.jsonl.
"""
import argparse
import csv
import json
import re
import statistics
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kbagent import account  # noqa: E402
from kbagent.agent import Agent  # noqa: E402
from kbagent.config import settings  # noqa: E402

RESULTS = ROOT / "eval" / "results"
GROUPS = ["основной", "перефразировка", "ловушки", "диалог", "счёт"]
DEFAULT_CLIENT = "anya"


def norm(s: str) -> str:
    s = s.lower().replace("ё", "е").replace("\xa0", " ").replace(" ", " ")
    s = re.sub(r"(\d)\.(\d)", r"\1,\2", s)        # 2.4 → 2,4
    s = re.sub(r"(\d)\s+%", r"\1%", s)             # 10 % → 10%
    s = re.sub(r"\*\*|__|_", "", s)                # разметка markdown
    return re.sub(r"\s+", " ", s)


def has_facts(text: str, must: list[str]) -> bool:
    t = norm(text)
    return all(any(norm(alt) in t for alt in item.split("|")) for item in must)


def has_none(text: str, banned: list[str]) -> bool:
    t = norm(text)
    return not any(norm(b) in t for b in banned)


def _num(value: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", value))


def covers(span: str | None, value: str) -> bool:
    """Покрывает ли часть или диапазон фрагмента («8–11») эталонную часть («9»)."""
    if not span:
        return False
    lo, _, hi = span.partition("–")
    return _num(lo) <= _num(value) <= _num(hi or lo)


def source_ok(sources: list[dict], gold: list[dict], must: list[str]) -> bool:
    for s in sources:
        if not s["cited"]:
            continue
        for g in gold:
            if s["doc_id"] != g["doc"]:
                continue
            if g["doc"] == "faq_npd":
                # разъяснение ФНС засчитывается, если в нём самом (вопрос ФНС и ответ) есть факты ответа
                if has_facts(f"{s['label']} {s['text']}", must):
                    return True
                continue
            if "article" in g and s.get("article") != g["article"]:
                continue
            if "part" in g and not covers(s.get("clause"), g["part"]):
                continue
            return True
    return False


def ask(agent: Agent, q: dict, clients: dict):
    client = clients.get(q.get("client", DEFAULT_CLIENT))
    history = []
    for prev in q.get("history", []):            # предыдущие вопросы диалога задаются по-настоящему
        r = agent.ask(prev, client=client, history=history or None, user="eval")
        history.append({"question": r.asked or prev, "text": r.text,
                        "docs": [s["doc_id"] for s in r.sources if s["cited"]]})
    return agent.ask(q["question"], client=client, history=history or None, user="eval")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(ROOT / "eval" / "questions.yaml"))
    ap.add_argument("--out", default="latest")
    ap.add_argument("--threshold", type=float, help="порог отказа переранжировщика")
    ap.add_argument("--only", help="только эти вопросы через запятую")
    args = ap.parse_args()
    if args.threshold is not None:
        settings.rerank_threshold = args.threshold

    questions = yaml.safe_load(Path(args.questions).read_text(encoding="utf-8"))["questions"]
    if args.only:
        questions = [q for q in questions if q["id"] in args.only.split(",")]
    agent, clients = Agent(), account.load_clients()
    rows = []
    for q in questions:
        r = ask(agent, q, clients)
        must = q.get("must_include", [])
        row = {"id": q["id"], "group": q.get("group", "основной"), "type": q["type"], "question": q["question"],
               "asked": r.asked or "", "route": r.route, "status": r.status, "answer": r.text, "seconds": r.seconds,
               "best_score": r.stats.get("best_score"),
               "cited": "; ".join(s["label"] for s in r.sources if s["cited"])}
        if q["type"] == "doc":
            row["facts_ok"] = r.answered and has_facts(r.text, must) and has_none(r.text, q.get("must_exclude", []))
            row["source_ok"] = r.answered and source_ok(r.sources, q["source"], must)
            row["correct"] = row["facts_ok"] and row["source_ok"]
        elif q["type"] == "none":
            row["correct"] = not r.answered
        elif q["type"] == "account":
            row["facts_ok"] = r.route == "account" and has_facts(r.text, must)
            row["source_ok"] = any(s["cited"] for s in r.sources)
            row["correct"] = row["facts_ok"] and row["source_ok"]
        elif q["type"] == "clarify":
            row["correct"] = r.status == "clarify"
        else:
            row["correct"] = r.status == "info" and r.route == "help"
        rows.append(row)
        print(f"{'+' if row['correct'] else '-'} {q['id']} [{r.route}/{r.status}, {r.seconds}s] {q['question'][:70]}",
              flush=True)

    doc = [r for r in rows if r["type"] == "doc"]
    none = [r for r in rows if r["type"] == "none"]
    acc = [r for r in rows if r["type"] == "account"]
    clarify = [r for r in rows if r["type"] == "clarify"]
    info = [r for r in rows if r["type"] == "info"]
    answered = [r for r in rows if r["status"] == "answer"]
    count = lambda items, key="correct": f"{sum(bool(r.get(key)) for r in items)} из {len(items)}"  # noqa: E731
    m = {
        "Верных ответов на вопросы по закону (факты и ссылка верны)": count(doc),
        "Ответов с верными фактами": count(doc, "facts_ok"),
        "Ответов со ссылкой на верную статью или разъяснение": count(doc, "source_ok"),
        "Ответов со ссылкой на источник (среди всех данных ответов)":
            f"{sum(1 for r in answered if r['cited'])} из {len(answered)}",
        "Неверных ответов, выданных как ответ":
            f"{sum(1 for r in doc if r['status'] == 'answer' and not r['facts_ok'])} из {len(doc)}",
        "Честных отказов на вопросы, ответа на которые в базе нет": count(none),
        "Ответов по счёту клиента (расчёт и ссылка на закон)": count(acc),
        "Встречных вопросов на слишком общий вопрос": count(clarify),
        "Справок на разговорные вопросы": count(info),
        "Время ответа, медиана": f"{statistics.median(r['seconds'] for r in rows):.1f} с",
        "Время ответа, максимум": f"{max(r['seconds'] for r in rows):.1f} с",
    }
    m = {k: v for k, v in m.items() if not v.endswith(" из 0")}
    total_ok = sum(r["correct"] for r in rows)
    groups = []
    for g in [g for g in GROUPS if any(r["group"] == g for r in rows)]:
        gi = [r for r in rows if r["group"] == g]
        groups.append(f"| {g} | {sum(r['correct'] for r in gi)} из {len(gi)} |")

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    md = [f"# Результаты на эталонном наборе ({stamp})", "",
          f"Набор: {Path(args.questions).name}, вопросов: {len(rows)}, всего верно: **{total_ok} из {len(rows)}**; "
          f"модель: {settings.llm_model}; порог отказа: {settings.rerank_threshold}", "",
          "| Метрика | Значение |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in m.items()]
    md += ["", "| Группа | Верно |", "|---|---|"] + groups
    md += ["", "| № | Вопрос | Итог | Статус | Время, с | Источник |", "|---|---|---|---|---|---|"]
    for r in rows:
        cited = r["cited"].replace("|", "/")[:160] or "—"
        md.append(f"| {r['id']} | {r['question']} | {'верно' if r['correct'] else 'ошибка'} | {r['status']} "
                  f"| {r['seconds']} | {cited} |")
    (RESULTS / f"{args.out}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    with open(RESULTS / f"{args.out}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) + ["facts_ok", "source_ok"], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(RESULTS / f"{args.out}.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\nВсего верно: {total_ok} из {len(rows)}")
    for k, v in m.items():
        print(f"{k}: {v}")
    print("\n".join(groups))


if __name__ == "__main__":
    main()
