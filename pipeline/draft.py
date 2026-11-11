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


# ---------------------------------------------------------------- запрос для Jira

def jira_query(units: List[Dict[str, Any]]) -> str:
    keys = sorted({u["task"] for u in units if u.get("task")})
    return "key in (" + ", ".join(keys) + ")" if keys else ""
