"""Файл проверки человеком: output/<набор>/validation.txt.

Код предлагает решение по каждой задаче. Человек правит вторую колонку, где не согласен, и ставит
'approved: yes'. Только после этого запускается агент.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import read_text, save_text

DECISIONS = {
    "doc": "править документацию и строка в release notes",
    "rn": "только строка в release notes (исправление ошибки)",
    "langs": "строка в release notes и проверить список языков",
    "screens": "текст не менять, проверить скриншоты",
    "skip": "не брать в работу",
    "agent": "решение не подтверждено, проверяет агент",
}
OF_ACTION = {"doc_and_rn": "doc", "rn_and_check_doc": "rn", "rn_and_check_langs": "langs",
             "screenshot_check": "screens", "skip": "skip", "review": "agent"}
ACTION_TEXT = {"doc_and_rn": "в доку + RN", "rn_and_check_doc": "RN + проверить доку",
               "rn_and_check_langs": "RN + проверить список языков", "screenshot_check": "проверить скрины",
               "skip": "отброшено", "review": "не определено"}
FILE_NAME = "validation.txt"


def proposal(unit: Dict[str, Any], conf: Dict[str, Any]) -> str:
    return "agent" if conf["level"] == "low" else OF_ACTION[unit["decision"]["action"]["value"]]


def rule_text(unit: Dict[str, Any]) -> str:
    d = unit["decision"]
    return (f"{ACTION_TEXT[d['action']['value']]} ({d['product']['rule']}, {d['visibility']['rule']}, "
            f"{d['class_draft']['value']})").replace("—", "-")


def render(dataset: str, release: str, units: List[Dict[str, Any]], conf: Dict[str, Dict[str, Any]],
           keep: Optional[Dict[str, str]] = None, approved: bool = False) -> str:
    keep = keep or {}
    L = [
        f"# Проверка решений перед запуском агента. Набор данных: {dataset}, релиз {release}.",
        "#",
        "# Как пользоваться:",
        "# 1. Откройте draft.md рядом с этим файлом и посмотрите, что собрал код.",
        "# 2. Если не согласны с решением по задаче, поменяйте слово во второй колонке.",
        "# 3. Когда все проверено, замените ниже 'approved: no' на 'approved: yes'.",
        "# 4. Запустите агента: python3 -m pipeline agent " + dataset,
        "#",
        "# Допустимые решения:",
    ]
    L += [f"#   {k:<7} - {v}" for k, v in DECISIONS.items()]
    L += ["#", "# Агент работает только с задачами doc, langs, agent и с задачами rn, у которых в документации",
          "# нашлись старые тексты интерфейса. Задачи skip и screens он не трогает.", "",
          f"approved: {'yes' if approved else 'no'}", "",
          "# задача | решение | решение правил | основание для проверки (низкая достоверность)"]
    for u in units:
        c = conf[u["id"]]
        why = "; ".join(f"{f['rule']}: {f['text']}" for f in c["flags"] if f["kind"] == "decision")
        L.append(f"{u['id']} | {keep.get(u['id'], proposal(u, c))} | {rule_text(u)} | {why}")
    return "\n".join(L) + "\n"


def parse(text: str) -> Dict[str, Any]:
    approved = False
    decisions: Dict[str, str] = {}
    auto: Dict[str, str] = {}
    errors: List[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.lower().startswith("approved"):
            approved = s.split(":", 1)[-1].strip().lower() in ("yes", "да", "true", "1")
            continue
        parts = [p.strip() for p in s.split("|")]
        if len(parts) < 2:
            errors.append(f"строка {n}: не разобрана: {s}")
            continue
        if parts[1] not in DECISIONS:
            errors.append(f"строка {n}: неизвестное решение '{parts[1]}' у {parts[0]}. Допустимо: {', '.join(DECISIONS)}")
            continue
        decisions[parts[0]] = parts[1]
        auto[parts[0]] = parts[2] if len(parts) > 2 else ""
    return {"approved": approved, "decisions": decisions, "auto": auto, "errors": errors}


def write(path: Path, dataset: str, release: str, units, conf, force: bool = False) -> str:
    """Не затирает правки человека. Возвращает, что сделано: created | kept | updated."""
    old = read_text(path)
    if old is None or force:
        save_text(path, render(dataset, release, units, conf))
        return "created"
    prev = parse(old)
    same = all(prev["auto"].get(u["id"]) == rule_text(u) for u in units) and set(prev["decisions"]) == {u["id"] for u in units}
    if same:
        return "kept"
    # правила дали другой результат: решения человека сохраняем там, где предложение правил не изменилось
    keep = {u["id"]: prev["decisions"][u["id"]] for u in units
            if u["id"] in prev["decisions"] and prev["auto"].get(u["id"]) == rule_text(u)}
    save_text(path, render(dataset, release, units, conf, keep=keep, approved=False))
    return "updated"
