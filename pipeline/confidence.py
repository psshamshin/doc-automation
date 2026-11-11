"""Достоверность решения правил по задаче (группа правил И, см. docs/RULES.md).

Правила всегда выдают какое-то решение. Достоверность решения низкая, если выполнен хотя бы один
критерий вида decision: решение опирается на косвенные признаки или правила не сработали. Такая задача
передается агенту на проверку. При высокой достоверности агент решение не пересматривает.

Виды критериев:
* decision - не подтверждено само решение (продукт, видимость);
* place - решение подтверждено, но не определено место в документации: это замечание автору текста.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

# критерии группы И (docs/RULES.md)
RULE_OF = {"product_unclear": "И1", "visibility_unclear": "И2", "text_only": "И3", "shared_strings": "И4",
           "other_product_files": "И5", "class_default": "И6", "no_page": "И7", "many_pages": "И8"}
DOC_ACTIONS = ("doc_and_rn", "rn_and_check_doc", "rn_and_check_langs")
MANY_PAGES = 3


def _evidence_keys(u: Dict[str, Any]) -> set:
    i = u["i18n"]
    keys = {("a", x["locale"], x["value"]) for x in i["added"]}
    keys |= {("r", x["locale"], x["value"]) for x in i["removed"]}
    keys |= {("c", x["locale"], x["old"], x["new"]) for x in i["changed"]}
    return keys


def assess(units: List[Dict[str, Any]], rules: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    markers = rules["product"]["other_product_file_markers"]
    # текст считается общим, только если он привязан к другой задаче, решение по которой тоже опирается на тексты
    owners: Dict[tuple, List[str]] = defaultdict(list)
    for u in units:
        if u["decision"]["action"]["value"] in ("doc_and_rn", "rn_and_check_doc"):
            for k in _evidence_keys(u):
                owners[k].append(u["id"])

    out: Dict[str, Dict[str, Any]] = {}
    for u in units:
        d = u["decision"]
        action = d["action"]["value"]
        flags: List[Dict[str, str]] = []

        def add(code: str, kind: str, text: str) -> None:
            flags.append({"rule": RULE_OF[code], "code": code, "kind": kind, "text": text})

        if d["product"]["value"] == "needs_review":
            add("product_unclear", "decision", "правила не определили продукт: " + "; ".join(d["product"]["reasons"]))
        if action != "skip" or d["product"]["value"] == "needs_review":
            if d["visibility"]["value"] == "unknown":
                add("visibility_unclear", "decision",
                    "правила не определили, видит ли изменение пользователь: нет изменений в текстах интерфейса, "
                    "навигации и правах, в записях нет слов об интерфейсе")
        if action != "skip":
            rule = d["visibility"]["rule"]
            by_text = rule == "Г5" or (rule == "Г2" and d["visibility"]["reasons"][0].startswith("в тексте"))
            if by_text:
                add("text_only", "decision",
                    "видимость определена только по словам в записи (" + d["visibility"]["reasons"][0]
                    + "), в коде подтверждения нет")
            keys = _evidence_keys(u)
            # текст, который дословно назван в записи задачи, точно принадлежит ей
            said = " ".join(e["text"] for e in u["entries"] if e["_flags"].get("significant")).lower()
            own = {k for k in keys if any(len(x) >= 4 and x.lower() in said for x in k[2:])}
            if rule == "Г1" and keys and not own and all(len(set(owners[k]) - {u["id"]}) > 0 for k in keys):
                others = sorted({o for k in keys for o in owners[k]} - {u["id"]})
                add("shared_strings", "decision",
                    "все изменившиеся тексты интерфейса привязаны и к другим задачам (" + ", ".join(others[:6])
                    + "): возможно, это не изменение этой задачи")
            src = [f for f in u["files"] if f.get("relevant") and f["category"] == "source"]
            foreign = [f for f in src if any(m in f["path"] for m in markers) or not f["reachable_from_app"]]
            if d["product"]["value"] == "point2" and src and len(foreign) * 2 >= len(src):
                add("other_product_files", "decision",
                    f"{len(foreign)} из {len(src)} файлов кода значимых записей относятся к другому продукту "
                    "или не используются приложением")
            if d["class_draft"]["why"].endswith("(по умолчанию)"):
                add("class_default", "place", "вид изменения не определен, поставлен по умолчанию")
        if action in DOC_ACTIONS:
            n = len(u["doc_candidates"])
            if n == 0:
                add("no_page", "place", "страница документации не найдена")
            elif n > MANY_PAGES:
                add("many_pages", "place", f"слишком много страниц-кандидатов: {n}")

        need_agent = any(f["kind"] == "decision" for f in flags)
        out[u["id"]] = {"level": "low" if need_agent else "high", "flags": flags}
    return out
