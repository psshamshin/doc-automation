"""Правки, которые код вносит сам, до агента.

* заменяет версию интерфейса в рабочей копии документации;
* собирает черновик release notes из записей ченджлога по образцу прошлого релиза.
Сводка для человека - файл проверки review.md (pipeline/validation.py).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import docs as DOCS
from . import product as P
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


def jira_title(unit: Dict[str, Any]) -> str:
    return re.sub(r"^\s*(\[[^\]]+\]\s*)+", "", (unit.get("jira") or {}).get("summary") or "").strip()


def rn_source(unit: Dict[str, Any]) -> Dict[str, Any]:
    """Текст строки release notes: значимые записи списка изменений. Название задачи в Jira - дополнительно:
    оно внутреннее ('Доработка страницы ... в Point 3') и для пользователя не годится (изменение 17)."""
    return {"source": "список изменений", "jira": jira_title(unit),
            "texts": significant_texts(unit) or [e["text"].strip() for e in unit["entries"]]}


PENDING_NOTE = "ожидает решения агента"


def rn_block(unit: Dict[str, Any], titles: Dict[str, str], lang: str, flagged: bool, decision: str = "") -> List[str]:
    if decision == "langs":
        return [f"- **[{P.RN_TITLES[lang]['general']}]**: Изменен набор языков интерфейса. "
                f"<!-- {unit['id']}, какие языки добавлены, уточнит агент -->", ""]
    src = rn_source(unit)
    texts = src["texts"]
    note = f"<!-- {unit['id']}, источник: {src['source']}" + (f", {PENDING_NOTE}" if decision == "agent" else "") \
           + (", TODO перевод" if lang != "rus" else "") + " -->"
    label = section_label(unit, titles, lang)
    if len(texts) == 1:
        return [f"- **[{label}]**: {texts[0]} {note}", ""]
    out = [f"- **[{label}]**: {note}", ""]
    out += [f"    - {t}" for t in texts[:MAX_SUBLINES]]
    if len(texts) > MAX_SUBLINES:
        out.append(f"    - ... еще записей: {len(texts) - MAX_SUBLINES}")
    return out + [""]


def rn_preview(units: List[Dict[str, Any]], decisions: Dict[str, str], index: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Строки черновика release notes для файла проверки."""
    titles = page_titles(index, "rus")
    out = []
    for u in units:
        d = decisions.get(u["id"])
        if d not in ("doc", "rn", "langs"):
            continue
        src = rn_source(u)
        text = "Изменен набор языков интерфейса." if d == "langs" else src["texts"][0]
        out.append({"unit": u["id"], "section": "fixes" if d == "rn" else "changes",
                    "label": P.RN_TITLES["rus"]["general"] if d == "langs" else section_label(u, titles, "rus"),
                    "text": text, "more": 0 if d == "langs" else len(src["texts"]) - 1,
                    "source": "код" if d == "langs" else src["source"]})
    return out


# ---------------------------------------------------------------- переименования (Д4)

QUOTES = "«»\"“”„'"


def planned_renames(ds, units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Замены названий элементов интерфейса, которые код может внести сам.

    Условия: у того же ключа перевода изменился текст; старое или новое название дословно названо в
    значимой записи задачи (значит, это изменение именно этой задачи); старое название стоит в документации
    в кавычках. Замены только планируются: вносит их команда agent после проверки человеком.
    """
    lang_of = ds.rules["i18n"]["doc_lang_of_locale"]
    out = []
    for u in units:
        if u["decision"]["action"]["value"] not in ("doc_and_rn", "rn_and_check_doc"):
            continue
        said = " ".join(significant_texts(u)).lower()
        seen = set()
        for c in u["i18n"]["changed"]:
            lang = lang_of.get(c["locale"])
            old, new = c["old"].strip(), c["new"].strip()
            if not lang or len(old) < 4 or "{{" in old or "$t(" in old or (c["locale"], old, new) in seen:
                continue
            if old.lower() not in said and new.lower() not in said:
                continue
            seen.add((c["locale"], old, new))
            rx = re.compile("([" + re.escape(QUOTES) + "])" + re.escape(old) + "([" + re.escape(QUOTES) + "])")
            root = DOCS.workspace_root(ds.result_dir, lang)
            for p in sorted(root.rglob("*.md")):
                if P.RELEASE_NOTES_DIR in p.relative_to(root).parts:
                    continue
                for i, line in enumerate((read_text(p) or "").splitlines(), 1):
                    if rx.search(line):
                        out.append({"unit": u["id"], "lang": lang, "file": str(p.relative_to(root)), "line": i,
                                    "old": old, "new": new})
    return out


def apply_renames(ds, planned: List[Dict[str, Any]], decisions: Dict[str, str]) -> List[Dict[str, Any]]:
    """Вносит запланированные замены для задач, которые человек принял. Повторный запуск ничего не меняет."""
    done = []
    trace = ds.result_dir / "trace.md"
    ttext = read_text(trace) or ""
    for r in planned:
        if decisions.get(r["unit"]) not in ("doc", "rn"):
            continue
        p = DOCS.workspace_root(ds.result_dir, r["lang"]) / r["file"]
        text = read_text(p)
        if text is None:
            continue
        rx = re.compile("([" + re.escape(QUOTES) + "])" + re.escape(r["old"]) + "([" + re.escape(QUOTES) + "])")
        new_text = rx.sub(lambda m: m.group(1) + r["new"] + m.group(2), text)
        if new_text != text:
            save_text(p, new_text)
        done.append(r)
        row = f"| {r['unit']} | {r['lang']} | {r['file']} | (код) «{r['old']}» -> «{r['new']}» | Д4, Е3 |"
        if row not in ttext:
            ttext += row + "\n"
    save_text(trace, ttext)
    return done


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
                        index: Dict[str, Any], final_lines: Optional[Dict[str, Dict[str, str]]] = None) -> List[Dict[str, Any]]:
    """Черновик release notes RU и EN по образцу файла прошлого релиза. Готовый текст (без метки черновика) не трогает."""
    v = ds.versions
    out = []
    for lang in ds.langs:
        root = DOCS.workspace_root(ds.result_dir, lang) / P.RELEASE_NOTES_DIR
        target = root / v["release_notes_file"]
        cur = read_text(target)
        if final_lines is None and cur is not None and DRAFT_MARK not in cur:
            out.append({"lang": lang, "file": f"{P.RELEASE_NOTES_DIR}/{target.name}", "status": "не тронут: метки черновика нет"})
            continue
        prev = read_text(root / v["previous_release_notes_file"]) if v["previous_release_notes_file"] else None
        titles = page_titles(index, lang)
        T = P.RN_TITLES[lang]
        L: List[str] = [] if final_lines is not None else [DRAFT_MARK, ""]
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
            L += [comp_title or "", "", *table, ""]
            if final_lines is None:
                L += [f"<!-- Версия {P.UI_COMPONENT} подставлена кодом. Остальные версии взяты из прошлого релиза: проверить. -->", ""]
        # задачи, ожидающие решения агента, входят в черновик с пометкой: агент проходит черновик один раз
        def is_fix(u):
            return u["decision"]["action"]["value"] == "rn_and_check_doc"
        waiting = [u for u in units if decisions.get(u["id"]) == "agent"] if final_lines is None else []
        changes = [u for u in units if decisions.get(u["id"]) in ("doc", "langs")] + [u for u in waiting if not is_fix(u)]
        fixes = [u for u in units if decisions.get(u["id"]) == "rn"] + [u for u in waiting if is_fix(u)]
        pending = [u["id"] for u in waiting]
        for title, group in ((T["changes"], changes), (T["fixes"], fixes)):
            if not group:
                continue
            L += [f"**{title}**", ""]
            for u in group:
                if final_lines is not None:
                    # итоговый файл: структуру собирает код, формулировку дал агент (изменение 18)
                    text = final_lines[u["id"]]["ru" if lang == "rus" else "en"].strip()
                    L += [f"- **[{section_label(u, titles, lang)}]**: {text}", ""]
                else:
                    L += rn_block(u, titles, lang, conf[u["id"]]["level"] == "low", decisions.get(u["id"], ""))
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
        out.append({"lang": lang, "file": f"{P.RELEASE_NOTES_DIR}/{target.name}",
                    "status": "собран из формулировок агента" if final_lines is not None else "черновик собран",
                    "changes": len(changes), "fixes": len(fixes), "pending": pending})
    return out


# ---------------------------------------------------------------- запрос для Jira

def jira_queries(units: List[Dict[str, Any]], changelog: Dict[str, Any]) -> Dict[str, str]:
    """Два запроса для Jira.

    exact - задачи, которые названы в списке изменений: по ним пайплайн принимает решения.
    control - задачи тех же проектов, закрытые в окне дат релиза, которых в списке изменений нет:
    так видно, что разработчики могли не записать.
    """
    keys = sorted({u["task"] for u in units if u.get("task")})
    if not keys:
        return {"exact": "", "control": ""}
    exact = "key in (" + ", ".join(keys) + ")"
    projects = sorted({k.split("-")[0] for k in keys})
    lo, hi = changelog.get("lower_date"), changelog.get("upper_date")
    control = ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", lo or "") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", hi or ""):
        control = (f"project in ({', '.join(projects)}) AND resolved >= \"{lo}\" AND resolved <= \"{hi} 23:59\" "
                   f"AND statusCategory = Done AND key not in ({', '.join(keys)})")
    return {"exact": exact, "control": control}


def jira_query_file(q: Dict[str, str], changelog: Dict[str, Any]) -> str:
    L = ["Запросы для Jira (Фильтры -> Расширенный поиск). Пайплайн составил их по списку изменений фронтенда.", "",
         "1. Основной: задачи, названные в списке изменений. Выгрузите результат (Экспорт -> XML) и положите файл в input/.", "",
         q["exact"], ""]
    if q["control"]:
        L += [f"2. Контрольный: задачи, закрытые с {changelog['lower_date']} по {changelog['upper_date']}, которых нет в списке "
              "изменений. Выгрузите так же и положите рядом: пайплайн покажет их в файле проверки отдельным списком.", "",
              q["control"], "",
              "Если результатов слишком много, сузьте запрос до задач с меткой продукта в названии, добавив в конец:",
              'AND summary ~ "point"', ""]
    return "\n".join(L)


def jira_extra(jira: Dict[str, Dict[str, Any]], units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Задачи из выгрузки Jira, которых нет в списке изменений."""
    known = {u["task"] for u in units if u.get("task")}
    return [{"key": k, "summary": (v.get("summary") or "").strip(), "tags": v.get("product_tags") or []}
            for k, v in sorted(jira.items()) if k not in known]
