"""Проверка ответов агента и внесение правок.

Агент ничего не меняет в документации. Он возвращает правки как данные: страница, точный существующий
фрагмент, новый текст. Код проверяет каждую правку по реальным данным и только после этого вносит ее.
Ответ, не прошедший проверку, не вносится целиком.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import docs as DOCS
from ..common import load_json, read_text, save_text
from .tasks import _stale_context, _unit_context, unit_pages, unit_sources

STATUSES = ("done", "unknown")      # unknown: в источниках нет фактов, вывода нет, ничего не вносится
DECISIONS = ("doc", "rn", "langs", "screens", "skip")
VIRTUAL_SOURCES = ("changelog", "i18n", "jira")
MAX_EDIT_LINES = 30
MAX_FIND_CHARS = 2000
APPROX_RATIO = 0.96
QUOTED_RE = re.compile(r"«([^»\n]{2,80})»|\"([^\"\n]{2,80})\"|“([^”\n]{2,80})”")


# ---------------------------------------------------------------- цитаты

def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _norm_code(text: str) -> str:
    """Код из патча без знаков '+' и '-' в начале строк: агент цитирует его без них."""
    return _norm("\n".join(re.sub(r"^[+-](?![+-])", "", line) for line in (text or "").splitlines()))


def _approx_in(nq: str, nt: str) -> bool:
    """Цитата с опечаткой в одну-две буквы: ищется по трем коротким опорным кускам, затем сверяется целиком."""
    n = len(nq)
    if n < 40:
        return False
    for pos in (0, n // 2 - 7, n - 15):
        anchor, start, seen = nq[pos:pos + 15], 0, 0
        while seen < 20:
            i = nt.find(anchor, start)
            if i < 0:
                break
            win = nt[max(0, i - pos): max(0, i - pos) + n + 2]
            if difflib.SequenceMatcher(None, nq, win, autojunk=False).ratio() >= APPROX_RATIO:
                return True
            start, seen = i + 1, seen + 1
    return False


def quote_found(quote: str, text: str, code: bool = False) -> str:
    """'exact' - цитата есть буква в букву (регистр не важен); 'lines' - каждая ее строка есть в тексте (пропущена
    служебная строка между ними); 'approx' - различие в одну-две буквы; '' - нет. Выдуманная фраза не проходит."""
    conv = _norm_code if code else _norm
    nq, nt = conv(quote).lower(), conv(text).lower()
    if nq in nt or _norm(quote).lower() in _norm(text).lower():
        return "exact"
    lines = [x for x in (conv(l).lower() for l in quote.splitlines()) if len(x) >= 3]
    if len(lines) > 1 and all(x in nt for x in lines):
        return "lines"
    return "approx" if _approx_in(nq, nt) else ""


def _virtual_text(u: Dict[str, Any], source: str) -> str:
    if source == "changelog":
        return " \n ".join(e["text"] for e in u["entries"])
    if source == "i18n":
        i = u["i18n"]
        vals = [x["value"] for x in i["added"] + i["removed"]] + [y for c in i["changed"] for y in (c["old"], c["new"])]
        return " \n ".join(vals)
    j = u.get("jira") or {}
    return f"{j.get('summary') or ''} \n {j.get('description') or ''}"


def _ui_vocabulary(ds) -> Dict[str, set]:
    """Все тексты интерфейса новой версии по языкам документации."""
    if not hasattr(ds, "_vocab"):
        lang_of = ds.rules["i18n"]["doc_lang_of_locale"]
        vocab: Dict[str, set] = {lang: set() for lang in lang_of.values()}
        for r in load_json(ds.data_dir / "03_i18n_rows.json")["new"]:
            if lang_of.get(r["locale"]):
                vocab[lang_of[r["locale"]]].add(_norm(r["value"]))
        ds._vocab = vocab
    return ds._vocab


# ---------------------------------------------------------------- документация в памяти

class Docs:
    """Принятая копия документации (result/): правки проверяются и вносятся по одной, каждая видит результат прошлой."""

    def __init__(self, ds):
        self.ds, self.cache, self.dirty = ds, {}, set()

    def path(self, f: str) -> Path:
        return self.ds.result_dir / f[len("docs/"):]

    def text(self, f: str) -> Optional[str]:
        if f not in self.cache:
            self.cache[f] = read_text(self.path(f))
        return self.cache[f]

    def put(self, f: str, text: str) -> None:
        self.cache[f] = text
        self.dirty.add(f)

    def save(self) -> None:
        for f in sorted(self.dirty):
            save_text(self.path(f), self.cache[f] or "")
        self.dirty.clear()


# ---------------------------------------------------------------- проверка ответа

def check_answer(path: Path, item: Dict[str, Any]) -> Dict[str, Any]:
    """Формальная проверка: файл есть, это JSON, обязательные поля на месте."""
    raw = read_text(path)
    if raw is None:
        return {"ok": False, "missing": True, "error": "ответа нет"}
    try:
        a = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"ответ не является JSON: {e}"}
    if not isinstance(a, dict):
        return {"ok": False, "error": "ответ должен быть объектом JSON"}
    if item["kind"] == "release_notes":
        a.setdefault("status", "done")
        a.setdefault("summary", "")
    miss = [k for k in ("status", "summary") if k not in a]
    if miss:
        return {"ok": False, "error": "в ответе нет полей: " + ", ".join(miss)}
    if a["status"] not in STATUSES:
        return {"ok": False, "error": f"status должен быть done или unknown, получено: {a['status']}"}
    if item["kind"] == "decide" and a["status"] == "done" and a.get("decision") not in DECISIONS:
        return {"ok": False, "error": "для задания decide нужно поле decision: " + ", ".join(DECISIONS)}
    return {"ok": True, "answer": a}


def verify_answer(ds, u: Dict[str, Any], kind: str, answer: Dict[str, Any], docs: Docs, conf: Dict[str, Any], item_id: str) -> Dict[str, Any]:
    """Сверяет ответ с реальными данными: каждая правка проверяется отдельно, плохая правка не губит остальные.

    Правка принимается, если: страница входит в страницы задачи; заменяемый фрагмент есть на ней ровно один раз;
    новый текст не длиннее MAX_EDIT_LINES строк, без комментариев и меток скриншотов; названия в кавычках существуют в
    интерфейсе; есть дословная цитата из источников задачи. Правки проверяются по порядку на копии страницы:
    вторая видит результат первой. Решение (для decide) принимается, если подтверждено цитатой.

    Возвращает: ops - принятые правки; bad - отклоненные правки с причинами; decision_problems - причины, по которым
    решение не принято; notes - замечания (шаги ссылаются на файлы вне источников): на прием не влияют.
    """
    approx: List[str] = []
    sources, pages = unit_sources(ds, u), unit_pages(ds, u)
    printed = _stale_context(ds, u) if u.get("_stale") else _unit_context(ds, u, conf)       # как напечатано в задании
    known = _norm(" \n ".join(_virtual_text(u, k) for k in VIRTUAL_SOURCES))

    def found(quote: str, text: str, code: bool = False) -> bool:
        how = quote_found(quote, text, code)
        if how == "approx":
            approx.append(quote[:60])
        return bool(how)

    def in_source(src: str, quote: str) -> Optional[bool]:
        if src in VIRTUAL_SOURCES:
            return found(quote, _virtual_text(u, src)) or bool(printed and found(quote, printed))
        if src in sources:
            return found(quote, read_text(sources[src]) or "", code=True)
        if src in pages:
            return found(quote, read_text(docs.path(src)) or "")
        inside = [p for k, p in sources.items() if k.startswith(src.rstrip("/") + "/")]
        return any(found(quote, read_text(p) or "", code=True) for p in inside) if inside else None

    def evidence_problems(evs: Any) -> List[str]:
        if not isinstance(evs, list) or not evs:
            return ["нет подтверждения"]
        out = []
        for n, ev in enumerate(evs, 1):
            if not isinstance(ev, dict) or not ev.get("source") or not ev.get("quote"):
                out.append(f"подтверждение {n}: нужны поля source и quote")
                continue
            raw, src = str(ev["quote"]), str(ev["source"]).strip("`")
            if len(_norm(raw)) < 3:
                out.append(f"подтверждение {n}: цитата слишком короткая")
                continue
            ok = in_source(src, raw)
            if ok is None:
                out.append(f"подтверждение {n}: источник не входит в разрешенные для этой задачи: {src}")
            elif not ok:
                out.append(f"подтверждение {n}: цитаты нет в источнике {src}: «{_norm(raw)[:80]}»")
        return out

    notes: List[str] = []
    steps = answer.get("steps")
    if not isinstance(steps, list) or not steps:
        notes.append("шаги: нет списка steps")
    else:
        own = (f"tasks/{item_id}.md", "AGENTS.md", "progress.md")
        for n, st in enumerate(steps, 1):
            src = str(st.get("source", "")).strip("`") if isinstance(st, dict) else ""
            if src and src not in VIRTUAL_SOURCES and src not in sources and src not in pages and src not in own \
                    and not any(k.startswith(src.rstrip("/") + "/") for k in sources):
                notes.append(f"шаг {n}: открыт файл вне источников задачи: {src}")
    decision_problems = evidence_problems(answer.get("evidence")) if kind == "decide" else []

    ops: List[Dict[str, Any]] = []
    bad: List[Dict[str, Any]] = []
    staged: Dict[str, str] = {}
    edits = answer.get("edits") or []
    if not isinstance(edits, list):
        bad.append({"n": 0, "file": "", "what": "", "problems": ["edits должен быть списком"]})
        edits = []
    for n, e in enumerate(edits, 1):
        entry = {"n": n, "file": str(e.get("file", "")) if isinstance(e, dict) else "", "what": str(e.get("what", ""))[:80] if isinstance(e, dict) else "",
                 "problems": []}
        probs = entry["problems"]
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) and e.get(k) for k in ("file", "find", "what")) \
                or not isinstance(e.get("replace"), str):
            probs.append("нужны поля file, find, replace, what")
            bad.append(entry)
            continue
        f = entry["file"] = e["file"].strip("`")
        text = staged.get(f, docs.text(f)) if f in pages else None
        find, new = e["find"], e["replace"]
        if f not in pages:
            probs.append("страница не входит в страницы задания")
        elif text is None:
            probs.append("страницы нет")
        else:
            count = text.count(find)
            if count == 0:
                probs.append(f"фрагмента find нет на странице дословно: «{_norm(find)[:70]}»")
            elif count > 1:
                probs.append(f"фрагмент find встречается на странице {count} раза: возьми более длинный фрагмент")
            elif find == new:
                probs.append("новый текст совпадает со старым")
            if len(find) > MAX_FIND_CHARS or len(new.splitlines()) > MAX_EDIT_LINES:
                probs.append(f"правка слишком большая (не больше {MAX_EDIT_LINES} строк нового текста): раздели на несколько")
            if "SCREENSHOT-TODO" in new:
                probs.append("метки скриншотов не ставятся: скриншоты к проверке определяет код")
            if "<!--" in new and "<!--" not in find:
                probs.append("в новом тексте комментарий: вопросы и пояснения пишутся в ответе, а не в документации")
            m = re.search(r"MD_(\w+)/", f)
            vocab = _ui_vocabulary(ds).get(m.group(1) if m else "", set())
            page_norm = _norm(text)
            for line in new.splitlines():
                for t in QUOTED_RE.findall(line):
                    term = _norm(next(x for x in t if x))
                    if term not in vocab and term not in page_norm and term not in known:
                        probs.append(f"названия «{term}» нет ни в текстах интерфейса новой версии, ни на странице, ни в записях задачи")
        probs += evidence_problems(e.get("evidence"))
        if probs:
            bad.append(entry)
            continue
        staged[f] = text.replace(find, new, 1)
        ops.append({"file": f, "find": find, "replace": new, "what": e["what"]})
    problems = [f"решение: {p}" for p in decision_problems] + \
               [f"правка {b['n']} ({b['file']}, «{b['what']}»): {p}" for b in bad for p in b["problems"]]
    return {"problems": problems, "ops": ops, "bad": bad, "staged": staged, "approx": approx, "notes": notes,
            "decision_problems": decision_problems}


def apply_ops(ds, docs: Docs, uid: str, verified: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Вносит проверенные правки в result/ и записывает их в trace.md."""
    for f, text in verified["staged"].items():
        docs.put(f, text)
    docs.save()
    trace = ds.result_dir / "trace.md"
    t_text = read_text(trace) or ""
    for op in verified["ops"]:
        m = re.match(r"docs/MD_(\w+)/docs/(.+)$", op["file"])
        lang, page = (m.group(1), m.group(2)) if m else ("", op["file"])
        t_text += f"| {uid} | {lang} | {page} | {op['what'].replace('|', '/')} | агент, проверено кодом |\n"
    save_text(trace, t_text)
    return verified["ops"]


def verify_rn(answer: Dict[str, Any], expected: List[str]) -> Dict[str, Any]:
    """Release notes: строки проверяются по задачам. Хорошая строка принимается, плохая или отсутствующая - нет.

    У каждой ожидаемой задачи должна быть одна строка RU и EN либо причина в no_line. Строки задач, которых нет среди
    ожидаемых, игнорируются. Возвращает lines (принятые), skipped (задачи без строки с причиной), problems (по задачам).
    """
    problems: Dict[str, List[str]] = {}
    lines: Dict[str, Dict[str, str]] = {}
    for n, x in enumerate(answer.get("lines") or [], 1):
        if not isinstance(x, dict) or not x.get("task"):
            problems.setdefault(f"строка {n}", []).append("нужны поля task, ru, en")
            continue
        uid = str(x["task"])
        if uid in lines:
            continue                                  # лишние строки одной задачи игнорируются: берется первая
        row = {"ru": str(x.get("ru") or "").strip(), "en": str(x.get("en") or "").strip(), "section": str(x.get("section") or "").strip()}
        bad = []
        for lang in ("ru", "en"):
            t = row[lang]
            if not t or "\n" in t:
                bad.append(f"формулировка {lang} пустая или в несколько строк")
            if "`" in t or "<!--" in t or re.search(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b", t):
                bad.append(f"в формулировке {lang} осталось имя из кода или служебная пометка")
        if not re.search(r"[А-Яа-я]", row["ru"]) or re.search(r"[А-Яа-я]", row["en"]):
            bad.append("формулировки перепутаны по языкам или не переведены")
        if bad:
            problems[uid] = bad
        else:
            lines[uid] = row
    skipped: Dict[str, str] = {}
    for n, x in enumerate(answer.get("no_line") or [], 1):
        if not isinstance(x, dict) or not x.get("task") or not str(x.get("reason") or "").strip():
            problems.setdefault(f"no_line {n}", []).append("нужны поля task и reason")
        else:
            skipped[str(x["task"])] = str(x["reason"]).strip()
    for uid in expected:
        if uid not in lines and uid not in skipped and uid not in problems:
            problems[uid] = ["строки нет: дай строку или причину в no_line"]
    return {"problems": {u: p for u, p in problems.items() if u in expected or u.startswith(("строка", "no_line"))},
            "lines": {u: r for u, r in lines.items() if u in expected},
            "skipped": {u: w for u, w in skipped.items() if u in expected}}
