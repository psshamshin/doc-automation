"""Правки, которые вносит код: версия интерфейса, release notes, запросы Jira.

Release notes собираются только из формулировок, которые дал и подтвердил агент (pipeline/agent).
Структуру файла (заголовок, таблицу версий, разделы, метки) строит код по образцу прошлого релиза.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import docs as DOCS
from . import product as P
from .common import read_text, save_text


def page_titles(index: Dict[str, Any], lang: str) -> Dict[str, str]:
    out = {}
    for f in index.get(lang, {}).get("files", []):
        h1 = next((h["text"] for h in f["headings"] if h["level"] == 1), None)
        if h1:
            out[f["path"]] = re.sub(r"\s*\{[^}]*\}\s*$", "", h1).strip()
    return out


def jira_title(unit: Dict[str, Any]) -> str:
    """Название задачи в Jira без меток продукта. Оно внутреннее и для пользователя не годится: только подсказка."""
    return re.sub(r"^\s*(\[[^\]]+\]\s*)+", "", (unit.get("jira") or {}).get("summary") or "").strip()


def significant_texts(unit: Dict[str, Any]) -> List[str]:
    return [e["text"].strip() for e in unit["entries"] if e["_flags"].get("significant")]


def section_label(unit: Dict[str, Any], titles: Dict[str, str], lang: str) -> str:
    """Метка раздела по умолчанию: название страницы задачи, если она одна, иначе «Общее»."""
    cands = [c["doc"] for c in unit["doc_candidates"]]
    if len(cands) == 1 and cands[0] in titles:
        return titles[cands[0]]
    return P.RN_TITLES[lang]["general"]


def section_choices(index: Dict[str, Any]) -> List[str]:
    """Разделы, из которых агент выбирает метку строки: «Общее» и названия страниц руководства."""
    titles = page_titles(index, "rus")
    return [P.RN_TITLES["rus"]["general"]] + sorted({t for p, t in titles.items() if p.startswith("user-manual/")})


def agent_label(section: Optional[str], index: Dict[str, Any], lang: str) -> Optional[str]:
    """Метка раздела на нужном языке по русскому названию, которое выбрал агент."""
    if not section:
        return None
    if section == P.RN_TITLES["rus"]["general"]:
        return P.RN_TITLES[lang]["general"]
    ru = page_titles(index, "rus")
    page = next((p for p, t in ru.items() if t == section), None)
    return page_titles(index, lang).get(page) if page else None


def is_fix_unit(unit: Dict[str, Any], decisions: Dict[str, str]) -> bool:
    """В «Исправленные ошибки» идет задача с решением «только RN» или такая, у которой все записи - исправления."""
    if decisions.get(unit["id"]) == "rn":
        return True
    sig = [e for e in unit["entries"] if e["_flags"].get("significant")]
    return bool(sig) and all(e["type"] == "fix" for e in sig)


def rn_preview(units: List[Dict[str, Any]], decisions: Dict[str, str], index: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Что войдет в release notes: по задаче - запись разработчиков и раздел. Для файла проверки."""
    titles = page_titles(index, "rus")
    out = []
    for u in units:
        if decisions.get(u["id"]) not in ("doc", "rn", "langs"):
            continue
        texts = significant_texts(u) or [e["text"].strip() for e in u["entries"]]
        out.append({"unit": u["id"], "section": "fixes" if is_fix_unit(u, decisions) else "changes",
                    "label": section_label(u, titles, "rus"), "text": texts[0], "more": len(texts) - 1})
    return out


# ---------------------------------------------------------------- версия интерфейса

def bump_versions(ds, component_versions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Заменяет версию компонента в рабочей копии документации. Повторный запуск ничего не меняет."""
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


# ---------------------------------------------------------------- release notes

def write_release_notes(ds, units: List[Dict[str, Any]], decisions: Dict[str, str], index: Dict[str, Any],
                        lines: Dict[str, Dict[str, str]]) -> List[str]:
    """Собирает файлы release notes RU и EN. lines: задача -> {ru, en, section}.

    Структура по образцу файла прошлого релиза: заголовок, таблица версий (версия интерфейса обновлена),
    разделы «Изменения» и «Исправленные ошибки». Возвращает пути собранных файлов.
    """
    v = ds.versions
    written = []
    for lang in ds.langs:
        root = DOCS.workspace_root(ds.result_dir, lang) / P.RELEASE_NOTES_DIR
        target = root / v["release_notes_file"]
        prev = read_text(root / v["previous_release_notes_file"]) if v["previous_release_notes_file"] else None
        titles, T = page_titles(index, lang), P.RN_TITLES[lang]
        L: List[str] = [f"# [{v['release']}] {v['release_date'] or 'ДД.ММ.ГГГГ'} {{-}}", ""]
        if prev:
            rows = prev.splitlines()
            ti = [i for i, l in enumerate(rows) if l.lstrip().startswith("|")]
            if ti:
                comp_title = next((rows[i] for i in range(ti[0] - 1, -1, -1) if rows[i].strip().startswith("**")), "")
                rx = re.compile(r"(\|\s*" + re.escape(P.UI_COMPONENT) + r"\s*\|\s*)(\S+)(\s*)(\|)")

                def repl(m):
                    pad = max(len(m.group(2)) + len(m.group(3)) - len(v["ui_new"]), 1)
                    return m.group(1) + v["ui_new"] + " " * pad + m.group(4)
                L += [comp_title, "", *[rx.sub(repl, r) for r in rows[ti[0]:ti[-1] + 1]], ""]
        accepted = [u for u in units if decisions.get(u["id"]) in ("doc", "langs", "rn") and u["id"] in lines]
        for title, group in ((T["changes"], [u for u in accepted if not is_fix_unit(u, decisions)]),
                             (T["fixes"], [u for u in accepted if is_fix_unit(u, decisions)])):
            if not group:
                continue
            L += [f"**{title}**", ""]
            for u in group:
                row = lines[u["id"]]
                label = agent_label(row.get("section"), index, lang) or section_label(u, titles, lang)
                L += [f"- **[{label}]**: {row['ru' if lang == 'rus' else 'en'].strip()}", ""]
        if prev and "\\clearpage" in prev:
            L += ["", "\\clearpage", ""]
        save_text(target, "\n".join(L).rstrip("\n") + "\n")
        pages = root / ".pages"
        ptxt = read_text(pages)
        if ptxt is not None and target.name not in ptxt and v["previous_release_notes_file"]:
            m = re.search(r"^(\s*-\s*)" + re.escape(v["previous_release_notes_file"]) + r"\s*$", ptxt, flags=re.M)
            if m:
                save_text(pages, ptxt[:m.start()] + f"{m.group(1)}{target.name}\n" + ptxt[m.start():])
        written.append(str(target))
    return written


# ---------------------------------------------------------------- запросы для Jira

def jira_queries(units: List[Dict[str, Any]], changelog: Dict[str, Any]) -> Dict[str, str]:
    """exact - задачи из списка изменений; control - закрытые в окне дат релиза задачи тех же проектов,
    которых в списке изменений нет."""
    keys = sorted({u["task"] for u in units if u.get("task")})
    if not keys:
        return {"exact": "", "control": ""}
    projects = sorted({k.split("-")[0] for k in keys})
    lo, hi = changelog.get("lower_date"), changelog.get("upper_date")
    control = ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", lo or "") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", hi or ""):
        control = (f"project in ({', '.join(projects)}) AND resolved >= \"{lo}\" AND resolved <= \"{hi} 23:59\" "
                   f"AND statusCategory = Done AND key not in ({', '.join(keys)})")
    return {"exact": "key in (" + ", ".join(keys) + ")", "control": control}


def jira_query_file(q: Dict[str, str], changelog: Dict[str, Any]) -> str:
    L = ["Запросы для Jira (Фильтры -> Расширенный поиск). Составлены по списку изменений фронтенда.", "",
         "1. Основной: задачи из списка изменений. Экспорт -> XML, файл положить в input/.", "", q["exact"], ""]
    if q["control"]:
        L += [f"2. Контрольный: задачи, закрытые с {changelog['lower_date']} по {changelog['upper_date']}, которых нет в списке "
              "изменений. Выгрузить так же: пайплайн покажет их отдельным списком.", "", q["control"], "",
              "Если результатов слишком много, добавьте в конец: AND summary ~ \"point\"", ""]
    return "\n".join(L)


def jira_extra(jira: Dict[str, Dict[str, Any]], units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Задачи из выгрузки Jira, которых нет в списке изменений."""
    known = {u["task"] for u in units if u.get("task")}
    return [{"key": k, "summary": (v.get("summary") or "").strip(), "tags": v.get("product_tags") or []}
            for k, v in sorted(jira.items()) if k not in known]
