"""Файл проверки человеком: output/<набор>/review.md. Один файл: и сводка, и решения.

В таблице три отметки на задачу: принять решение правил, отклонить его или передать задачу агенту.
Агент запускается только после отметки 'Проверено: [x]'.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import product as P
from .common import load_json, read_text, save_text

FILE_NAME = "review.md"
OF_ACTION = {"doc_and_rn": "doc", "rn_and_check_doc": "rn", "rn_and_check_langs": "langs",
             "screenshot_check": "screens", "skip": "skip", "review": "agent"}
ACTION_TEXT = {"doc_and_rn": "в доку + RN", "rn_and_check_doc": "RN + проверить доку",
               "rn_and_check_langs": "RN + проверить список языков", "screenshot_check": "проверить скрины",
               "skip": "отброшено", "review": "не определено"}
DECISION_TEXT = {"doc": "править документацию и строка в release notes", "rn": "строка в release notes",
                 "langs": "строка в release notes и сверка списка языков", "screens": "проверить скриншоты",
                 "skip": "не брать в работу", "agent": "проверяет агент"}
MARKS = ("accept", "reject", "agent")
ON_RE = re.compile(r"\[\s*[xXхХ✓vV+]\s*\]")
BOX_RE = re.compile(r"\[\s*[xXхХ✓vV+ ]?\s*\]")
ID_RE = re.compile(r"((?:[A-Z][A-Z0-9]+-\d+)|NOTASK:[^\s\]|)]+)")
SUMMARY_LEN = 110


def jira_template(ds) -> str:
    """Шаблон адреса задачи: dataset.json набора данных, иначе knowledge/links.json, поверх него локальный
    knowledge/local.json (в git не попадает). Пустая строка - ссылок нет."""
    if "jira_task_url" in ds.overrides:
        return ds.overrides["jira_task_url"] or ""
    links: Dict[str, Any] = {}
    for name in (P.LINKS_FILE, P.LOCAL_LINKS_FILE):
        p = Path(ds.base) / name
        if p.is_file():
            links.update({k: v for k, v in load_json(p).items() if v})
    return links.get("jira_task_url") or ""


def task_url(template: str, unit_id: Optional[str]) -> Optional[str]:
    return template.replace("{key}", unit_id) if template and unit_id else None


def rule_text(unit: Dict[str, Any]) -> str:
    d = unit["decision"]
    return (f"{ACTION_TEXT[d['action']['value']]} ({d['product']['rule']}, {d['visibility']['rule']}, "
            f"{d['class_draft']['value']})").replace("—", "-")


def default_mark(unit: Dict[str, Any], conf: Dict[str, Any]) -> str:
    return "agent" if conf["level"] == "low" or unit["decision"]["action"]["value"] == "review" else "accept"


def decision_of(unit: Dict[str, Any], mark: str) -> str:
    """Отметка человека -> что делать с задачей.

    accept - решение правил; reject - задача в работу не берется, а если правила ее отбросили,
    то задачу проверяет агент; agent - решение проверяет агент.
    """
    action = unit["decision"]["action"]["value"]
    if mark == "accept":
        return OF_ACTION[action]
    if mark == "reject":
        return "agent" if action == "skip" else "skip"
    return "agent"


def _cell(s: str) -> str:
    return (s or "").replace("|", "/").replace("\n", " ").strip()


def summary_of(unit: Dict[str, Any]) -> str:
    sig = [e for e in unit["entries"] if e["_flags"].get("significant")]
    pool = sig or unit["entries"]
    text = _cell(pool[0]["text"]) if pool else ""
    if len(text) > SUMMARY_LEN:
        text = text[:SUMMARY_LEN].rstrip() + "..."
    more = len(pool) - 1
    return text + (f" (и еще записей: {more})" if more > 0 else "")


def render(ds, units: List[Dict[str, Any]], conf: Dict[str, Dict[str, Any]], code_edits: List[Dict[str, Any]],
           rn_files: List[Dict[str, Any]], jira_count: int, marks: Optional[Dict[str, str]] = None,
           reviewed: bool = False) -> str:
    marks = marks or {}
    v = ds.versions
    L = [f"# Проверка решений: релиз {v['release']} (набор данных {ds.name})", "",
         f"Проверено: [{'x' if reviewed else ' '}]", "",
         "Этот файл собран кодом. Агент запустится только после вашей проверки.", "",
         "1. В таблице ниже у каждой задачи стоит одна отметка `[x]`. Если не согласны, переставьте ее.",
         "2. Поставьте `x` в строке «Проверено» наверху: `Проверено: [x]`.",
         f"3. Запустите: `python3 -m pipeline agent {ds.name}`.", "",
         "| Отметка | Что значит |", "| --- | --- |",
         "| Принять | Согласен с решением правил |",
         "| Отклонить | Не согласен: задачу в работу не брать. Если правила задачу отбросили, ее проверит агент |",
         "| Агенту | Пусть решение проверит агент по коду |", "",
         "## Версии", "",
         "| Что | Значение | Откуда |", "| --- | --- | --- |",
         f"| Новый релиз | {v['release']} | {v['release_source']} |",
         f"| Дата релиза | {v['release_date'] or 'неизвестна'} | база знаний или release notes техписа |",
         f"| Документация предыдущего релиза | {v['docs_base_version'] or 'не определена'} | последний файл release notes |",
         f"| Интерфейс | {v['ui_old']} -> {v['ui_new']} | `{P.APP_PACKAGE_JSON}` в папках с кодом |",
         f"| Задач Jira в выгрузке | {jira_count} | `{P.SLOT_JIRA}/` |", ""]
    for n in v["notes"]:
        L.append(f"- **Внимание:** {n}")
    if jira_count == 0:
        L.append("- **Внимание:** выгрузки Jira нет, продукт определяется только по коду. Запрос для выгрузки: `jira_query.txt`.")
    L += ["", "## Что уже сделал код", ""]
    for b in code_edits:
        warn = (f" **В этом языке стояло {b['old']}, а не {b['expected_old']}: в прошлом релизе версию не обновили.**"
                if b["mismatch"] else "")
        L.append(f"- [{b['lang']}] `{b['file']}`: версия интерфейса `{b['variable']}` {b['old']} -> {b['new']}.{warn}")
    if not code_edits:
        L.append(f"- Переменная `{P.UI_VERSION_VARIABLE}` в документации не найдена, версия не обновлена.")
    for r in rn_files:
        L.append(f"- [{r['lang']}] Черновик release notes: `{P.OUT_RESULT}/MD_{r['lang']}/docs/{r['file']}` ({r['status']}).")
    L += ["", "## Решения по задачам", "",
          "| Задача | Что изменилось | Решение правил | Основание для проверки | Принять | Отклонить | Агенту |",
          "| --- | --- | --- | --- | :-: | :-: | :-: |"]
    order = {"doc": 0, "rn": 1, "langs": 2, "screens": 3, "agent": 4, "skip": 5}

    def key(u):
        low = conf[u["id"]]["level"] == "low"
        return (4 if low else order[OF_ACTION[u["decision"]["action"]["value"]]], u["id"])

    template = jira_template(ds)
    for u in sorted(units, key=key):
        c = conf[u["id"]]
        url = task_url(template, u.get("task"))
        name = f"[{u['id']}]({url})" if url else u["id"]
        why = "; ".join(f"{f['rule']}: {f['short']}" for f in c["flags"] if f["kind"] == "decision")
        m = marks.get(u["id"], default_mark(u, c))
        boxes = " | ".join("[x]" if m == k else "[ ]" for k in MARKS)
        L.append(f"| {name} | {summary_of(u)} | {rule_text(u)} | {why} | {boxes} |")
    L += ["", "Коды в скобках - сработавшие правила: В - продукт, Г - видимость, Д - вид изменения, И - достоверность "
          "решения. Расшифровка: `docs/RULES.md`. Все записи, файлы и тексты по каждой задаче: `report.md`.", ""]
    return "\n".join(L).replace("—", "-")


def parse(text: str) -> Dict[str, Any]:
    reviewed = False
    marks: Dict[str, str] = {}
    auto: Dict[str, str] = {}
    errors: List[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.lower().startswith("проверено"):
            reviewed = bool(ON_RE.search(s))
            continue
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 7 or not all(BOX_RE.fullmatch(c) for c in cells[-3:]):
            continue
        m = ID_RE.search(cells[0])
        if not m:
            continue
        uid = m.group(1)
        on = [k for k, c in zip(MARKS, cells[-3:]) if ON_RE.fullmatch(c)]
        if len(on) != 1:
            errors.append(f"строка {n}, {uid}: нужна ровно одна отметка [x], сейчас их {len(on)}")
            continue
        marks[uid] = on[0]
        auto[uid] = cells[2]
    return {"reviewed": reviewed, "marks": marks, "auto": auto, "errors": errors}


def decisions(val: Dict[str, Any], units: List[Dict[str, Any]]) -> Dict[str, str]:
    return {u["id"]: decision_of(u, val["marks"][u["id"]]) for u in units if u["id"] in val["marks"]}


def proposals(units: List[Dict[str, Any]], conf: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    return {u["id"]: decision_of(u, default_mark(u, conf[u["id"]])) for u in units}


def write(path: Path, ds, units, conf, code_edits, rn_files, jira_count: int, force: bool = False) -> str:
    """Не затирает отметки человека. Возвращает, что сделано: created | kept | updated."""
    old = read_text(path)
    if old is None or force:
        save_text(path, render(ds, units, conf, code_edits, rn_files, jira_count))
        return "created"
    prev = parse(old)
    same = all(prev["auto"].get(u["id"]) == rule_text(u) for u in units) and set(prev["marks"]) == {u["id"] for u in units}
    if same and not prev["errors"]:
        save_text(path, render(ds, units, conf, code_edits, rn_files, jira_count, marks=prev["marks"], reviewed=prev["reviewed"]))
        return "kept"
    # правила дали другой результат: отметки сохраняются там, где решение правил не изменилось
    keep = {u["id"]: prev["marks"][u["id"]] for u in units
            if u["id"] in prev["marks"] and prev["auto"].get(u["id"]) == rule_text(u)}
    save_text(path, render(ds, units, conf, code_edits, rn_files, jira_count, marks=keep, reviewed=False))
    return "updated"
