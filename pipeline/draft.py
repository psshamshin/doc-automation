"""Черновик, который код собирает сам, до агента.

* правит версию интерфейса в рабочей копии документации;
* собирает черновик release notes из записей ченджлога по образцу прошлого релиза;
* пишет draft.md: что определилось, что сделал код, у каких решений низкая достоверность и что останется агенту.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import docs as DOCS
from . import product as P
from . import validation as V
from .common import read_text, save_text

DRAFT_MARK = "<!-- ЧЕРНОВИК: собран кодом из ченджлога. Формулировки и перевод уточняет агент. -->"
MAX_SUBLINES = 8


# ---------------------------------------------------------------- вспомогательное

def page_titles(index: Dict[str, Any], lang: str) -> Dict[str, str]:
    out = {}
    for f in index.get(lang, {}).get("files", []):
        h1 = next((h["text"] for h in f["headings"] if h["level"] == 1), None)
        if h1:
            out[f["path"]] = re.sub(r"\s*\{[^}]*\}\s*$", "", h1).strip()
    return out


def significant_texts(unit: Dict[str, Any]) -> List[str]:
    return [e["text"].strip() for e in unit["entries"] if e["_flags"].get("significant")]


def section_label(unit: Dict[str, Any], titles: Dict[str, str], lang: str) -> str:
    cands = [c["doc"] for c in unit["doc_candidates"]]
    if len(cands) == 1 and cands[0] in titles:
        return titles[cands[0]]
    return P.RN_TITLES[lang]["general"]


def rn_block(unit: Dict[str, Any], titles: Dict[str, str], lang: str, flagged: bool, decision: str = "") -> List[str]:
    if decision == "langs":
        return [f"- **[{P.RN_TITLES[lang]['general']}]**: Изменен набор языков интерфейса. "
                f"<!-- {unit['id']}, какие языки добавлены, уточнит агент -->", ""]
    texts = significant_texts(unit) or [e["text"].strip() for e in unit["entries"]]
    note = f"<!-- {unit['id']}" + (", решение не подтверждено" if flagged else "") \
           + (", TODO перевод" if lang != "rus" else "") + " -->"
    label = section_label(unit, titles, lang)
    if len(texts) == 1:
        return [f"- **[{label}]**: {texts[0]} {note}", ""]
    out = [f"- **[{label}]**: {note}", ""]
    out += [f"    - {t}" for t in texts[:MAX_SUBLINES]]
    if len(texts) > MAX_SUBLINES:
        out.append(f"    - ... еще записей: {len(texts) - MAX_SUBLINES}")
    return out + [""]


# ---------------------------------------------------------------- правки кодом

def bump_versions(ds, component_versions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Д10: заменяет версию компонента в рабочей копии документации. Повторный запуск ничего не меняет."""
    done = []
    for cv in component_versions:
        rx = re.compile("(" + re.escape(cv["variable"]) + r"\s*=\s*)([\w.\-]+)")
        for lang in ds.langs:
            root = DOCS.workspace_root(ds.result_dir, lang)
            for h in cv.get("doc_mentions", {}).get(lang, []):
                p = root / h["file"]
                text = read_text(p)
                if text is None:
                    continue
                new_text = rx.sub(lambda m: m.group(1) + cv["new"], text)
                if new_text != text:
                    save_text(p, new_text)
                done.append({"lang": lang, "file": h["file"], "line": h["line"], "variable": cv["variable"],
                             "old": h["current"], "new": cv["new"], "expected_old": cv["old"],
                             "mismatch": h["current"] != cv["old"]})
    trace = ds.result_dir / "trace.md"
    text = read_text(trace) or ""
    for d in done:
        row = f"| (код) | {d['lang']} | {d['file']} | {d['variable']}: {d['old']} -> {d['new']} | Д10 |"
        if row not in text:
            text += row + "\n"
    save_text(trace, text)
    return done


def write_release_notes(ds, units: List[Dict[str, Any]], decisions: Dict[str, str], conf: Dict[str, Any],
                        index: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Черновик release notes RU и EN по образцу файла прошлого релиза. Готовый текст (без метки черновика) не трогает."""
    v = ds.versions
    out = []
    for lang in ds.langs:
        root = DOCS.workspace_root(ds.result_dir, lang) / P.RELEASE_NOTES_DIR
        target = root / v["release_notes_file"]
        cur = read_text(target)
        if cur is not None and DRAFT_MARK not in cur:
            out.append({"lang": lang, "file": f"{P.RELEASE_NOTES_DIR}/{target.name}", "status": "не тронут: метки черновика нет"})
            continue
        prev = read_text(root / v["previous_release_notes_file"]) if v["previous_release_notes_file"] else None
        titles = page_titles(index, lang)
        T = P.RN_TITLES[lang]
        L: List[str] = [DRAFT_MARK, ""]
        date = v["release_date"] or "ДД.ММ.ГГГГ"
        header = f"# [{v['release']}] {date} {{-}}"
        table: List[str] = []
        comp_title = None
        if prev:
            lines = prev.splitlines()
            ti = [i for i, l in enumerate(lines) if l.lstrip().startswith("|")]
            if ti:
                table = lines[ti[0]:ti[-1] + 1]
                comp_title = next((lines[i] for i in range(ti[0] - 1, -1, -1) if lines[i].strip().startswith("**")), None)
            rx = re.compile(r"(\|\s*" + re.escape(P.UI_COMPONENT) + r"\s*\|\s*)(\S+)(\s*)(\|)")

            def repl(m):
                pad = max(len(m.group(2)) + len(m.group(3)) - len(v["ui_new"]), 1)
                return m.group(1) + v["ui_new"] + " " * pad + m.group(4)
            table = [rx.sub(repl, l) for l in table]
        L += [header, ""]
        if table:
            L += [comp_title or "", "", *table, "",
                  f"<!-- Версия {P.UI_COMPONENT} подставлена кодом. Остальные версии взяты из прошлого релиза: проверить. -->", ""]
        changes = [u for u in units if decisions.get(u["id"]) in ("doc", "langs")]
        fixes = [u for u in units if decisions.get(u["id"]) == "rn"]
        pending = [u["id"] for u in units if decisions.get(u["id"]) == "agent"]
        for title, group in ((T["changes"], changes), (T["fixes"], fixes)):
            if not group:
                continue
            L += [f"**{title}**", ""]
            for u in group:
                L += rn_block(u, titles, lang, conf[u["id"]]["level"] == "low", decisions.get(u["id"], ""))
        if pending:
            L += ["<!-- Не решено, строки появятся после работы агента: " + ", ".join(pending) + " -->", ""]
        if prev and "\\clearpage" in prev:
            L += ["", "\\clearpage", ""]
        save_text(target, "\n".join(L).rstrip("\n") + "\n")
        # оглавление раздела
        pages = root / ".pages"
        ptxt = read_text(pages)
        if ptxt is not None and target.name not in ptxt and v["previous_release_notes_file"]:
            m = re.search(r"^(\s*-\s*)" + re.escape(v["previous_release_notes_file"]) + r"\s*$", ptxt, flags=re.M)
            if m:
                save_text(pages, ptxt[:m.start()] + f"{m.group(1)}{target.name}\n" + ptxt[m.start():])
        out.append({"lang": lang, "file": f"{P.RELEASE_NOTES_DIR}/{target.name}", "status": "черновик собран",
                    "changes": len(changes), "fixes": len(fixes), "pending": pending})
    return out


# ---------------------------------------------------------------- draft.md

def jira_query(units: List[Dict[str, Any]]) -> str:
    keys = sorted({u["task"] for u in units if u.get("task")})
    return "key in (" + ", ".join(keys) + ")" if keys else ""


def _cell(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def build(ds, units, conf, decisions, bumps, rn_files, index, jira_count: int) -> str:
    v = ds.versions
    titles = page_titles(index, "rus")
    name = ds.name
    L: List[str] = [f"# Черновик: релиз {v['release']} (набор данных {name})", ""]
    L += ["Этот файл собран кодом, агент еще не запускался. Порядок работы:", "",
          "1. Прочитайте черновик. Подробности по каждой задаче: `report.md`.",
          f"2. Проверьте решения в `{V.FILE_NAME}`, поправьте, с чем не согласны, поставьте `approved: yes`.",
          f"3. Запустите агента: `python3 -m pipeline agent {name}`. Он проверит задачи из раздела 4 и напишет текст "
          "для задач из раздела 5.", "",
          "Достоверность решения низкая, если правила не сработали или решение опирается только на косвенные "
          "признаки. Критерии: `docs/RULES.md`, группа И.", ""]

    L += ["## 1. Что определилось автоматически", "",
          "| Что | Значение | Откуда |", "| --- | --- | --- |",
          f"| Новый релиз | {v['release']} | {v['release_source']} |",
          f"| Дата релиза | {v['release_date'] or 'неизвестна'} | база знаний или release notes техписа |",
          f"| Документация предыдущего релиза | {v['docs_base_version'] or 'не определена'} | последний файл release notes |",
          f"| Интерфейс: было | {v['ui_old']} | `{P.SLOT_CODE_OLD}/{P.APP_PACKAGE_JSON}` |",
          f"| Интерфейс: стало | {v['ui_new']} | `{P.SLOT_CODE_NEW}/{P.APP_PACKAGE_JSON}` |",
          f"| Задач Jira в выгрузке | {jira_count} | `{P.SLOT_JIRA}/` |", ""]
    for n in v["notes"]:
        L.append(f"- **Внимание:** {n}")
    if jira_count == 0:
        q = jira_query(units)
        L += ["- **Внимание:** выгрузки Jira нет, продукт определяется только по коду.",
              f"  Запрос для выгрузки (Jira -> Фильтры -> Расширенный поиск): `{q}`"]
    L.append("")

    L += ["## 2. Что уже сделал код", ""]
    if bumps:
        for b in bumps:
            warn = f" **В доке стояло {b['old']}, а ожидалось {b['expected_old']}: проверить.**" if b["mismatch"] else ""
            L.append(f"- [{b['lang']}] `{b['file']}`:{b['line']}: `{b['variable']}` {b['old']} -> {b['new']}.{warn}")
    else:
        L.append(f"- Переменная `{P.UI_VERSION_VARIABLE}` в документации не найдена, версия не обновлена.")
    for r in rn_files:
        extra = f": изменений {r['changes']}, исправлений {r['fixes']}, не решено {len(r['pending'])}" if "changes" in r else ""
        L.append(f"- [{r['lang']}] `{P.OUT_RESULT}/MD_{r['lang']}/docs/{r['file']}`: {r['status']}{extra}.")
    L += ["", "Текст страниц документации код не пишет: это работа агента.", ""]

    L += ["## 3. Решения по задачам", "",
          "| Задача | Предложение | Решение правил | Достоверность | Страницы |", "| --- | --- | --- | --- | --- |"]
    order = {"doc": 0, "rn": 1, "langs": 2, "screens": 3, "agent": 4, "skip": 5}
    for u in sorted(units, key=lambda u: (order[decisions[u["id"]]], u["id"])):
        c = conf[u["id"]]
        pages = ", ".join(titles.get(x["doc"], x["doc"].split("/")[-1]) for x in u["doc_candidates"][:4])
        L.append(f"| {u['id']} | {decisions[u['id']]} | {V.rule_text(u)} | "
                 f"{'**низкая**' if c['level'] == 'low' else 'высокая'} | {_cell(pages)} |")
    L += ["", "Предложение: " + "; ".join(f"`{k}` - {t}" for k, t in V.DECISIONS.items()) + ".", ""]

    flagged = [u for u in units if conf[u["id"]]["level"] == "low"]
    L += ["## 4. Решения с низкой достоверностью: проверяет агент", ""]
    if not flagged:
        L += ["Таких решений нет.", ""]
    for u in flagged:
        L += [f"### {u['id']}", "", f"Решение правил: {V.rule_text(u)}.", "", "Основание для проверки:", ""]
        L += [f"- {f['rule']}: {f['text']}" for f in conf[u["id"]]["flags"]]
        L += ["", "Записи:", ""]
        for e in u["entries"]:
            mark = "" if e["_flags"].get("significant") else " *(не учитывается)*"
            L.append(f"- {_cell(e['text'])[:260]}{mark}")
        L.append("")

    work = [u for u in units if decisions[u["id"]] in ("doc", "rn", "langs", "screens")]
    L += ["## 5. Решения с высокой достоверностью", ""]
    for u in work:
        d = decisions[u["id"]]
        L += [f"### {u['id']}: {V.DECISIONS[d]}", ""]
        for t in significant_texts(u)[:MAX_SUBLINES]:
            L.append(f"- {_cell(t)[:260]}")
        cands = [x["doc"] for x in u["doc_candidates"]]
        if cands:
            L.append("- Страницы: " + ", ".join(f"`{x}`" for x in cands))
        i = u["i18n"]
        new_ru = [x["value"] for x in i["added"] if x["locale"] == "ru"][:6]
        if new_ru:
            L.append("- Новые тексты интерфейса: " + ", ".join(f"«{_cell(x)}»" for x in new_ru))
        for c in [c for c in i["changed"] if c["locale"] == "ru"][:6]:
            L.append(f"- Текст изменен: «{_cell(c['old'])}» -> «{_cell(c['new'])}»")
        for lang, hits in (u.get("doc_mentions") or {}).items():
            for needle, found in list(hits.items())[:4]:
                L.append(f"- Старый текст «{_cell(needle)}» есть в документации [{lang}]: "
                         + ", ".join(f"`{h['file']}`:{h['line']}" for h in found[:4]))
        place = [f["text"] for f in conf[u["id"]]["flags"] if f["kind"] == "place"]
        if place:
            L.append("- Замечания: " + "; ".join(place))
        agent_job = {"doc": "агент напишет текст правок", "langs": "агент сверит список языков",
                     "screens": "агент не нужен: проверьте скриншоты страниц вручную",
                     "rn": "агент проверит документацию" if u.get("doc_mentions") else
                           "агент не нужен: старых текстов в документации не найдено"}[d]
        L += [f"- Дальше: {agent_job}.", ""]

    dropped = [u for u in units if decisions[u["id"]] == "skip"]
    L += ["## 6. Отброшено", ""]
    for u in dropped:
        d = u["decision"]
        first = (u["entries"][0]["text"] if u["entries"] else "")[:140]
        L.append(f"- {u['id']}: {d['action']['why']} ({d['product']['rule']}, {d['visibility']['rule']}). {_cell(first)}")
    L.append("")
    return "\n".join(L).replace("—", "-")
