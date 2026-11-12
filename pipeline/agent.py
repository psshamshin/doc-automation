"""Обертка агента: изолированная рабочая папка, задания, проверка ответов, журнал.

Агент не работает в папке проекта. Для него собирается отдельная папка output/<набор>/agent_workspace/,
в которой есть только то, что нужно для заданий: сами задания, рабочая копия документации и источники
каждой задачи (патчи и файлы кода). Правил отбора, кода пайплайна, чужих данных и документации техписа
там нет.

Что делает код вокруг агента:
* проверяет ответ: каждое утверждение должно быть подтверждено дословной цитатой из источников задачи;
* принимает правки документации только из подтвержденных ответов, остальные откладывает в agent/rejected/;
* ведет журнал agent/summary.md: что решено, что принято, что отклонено и почему.

Виды заданий: decide (проверить решение), write_doc (правка RU и EN), check_doc (исправление: проверить
документацию), check_langs (сверить список языков), release_notes (довести черновик).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import docs as DOCS
from . import draft as DRAFT
from . import product as P
from . import validation as V
from .common import ROOT, load_json, log, read_text, save_json, save_text

# Агент не выбран: по умолчанию задания только готовятся. Команда задается параметром --cmd или переменной
# окружения DOC_AGENT_CMD. Она запускается в рабочей папке агента, получает текст задания на стандартный
# ввод и должна записать файл ответа.
PROMPTS = ROOT / "agent" / "prompts"
KIND_ORDER = ["decide", "write_doc", "check_doc", "check_langs", "release_notes"]
ANSWER_REQUIRED = ["item", "status", "summary"]
STATUSES = ("done", "needs_human", "recheck")
FINAL = ("done", "recheck")   # вывод сделан; recheck - агент сам просит второй круг
NEED_EVIDENCE = ("decide", "write_doc", "check_doc", "check_langs")
VIRTUAL_SOURCES = ("changelog", "i18n", "jira")
LOG_FILES = ("docs/trace.md", "docs/screenshots.md")
LANG_SETTINGS = ("apps/point/package.json", "apps/point/src/app/translations/i18n.ts")


def agent_cmd(cmd: Optional[str]) -> List[str]:
    cmd = cmd or os.environ.get("DOC_AGENT_CMD")
    return shlex.split(cmd) if cmd else []


def workspace(ds) -> Path:
    return ds.run_dir / P.OUT_WORKSPACE


def unit_dir(uid: str) -> str:
    return uid.replace(":", "_").replace("#", "_")


# ---------------------------------------------------------------- очередь

def kinds_for(decision: str, unit: Dict[str, Any]) -> List[str]:
    if decision == "agent":
        return ["decide"]
    if decision == "doc":
        return ["write_doc"]
    if decision == "langs":
        return ["check_langs"]
    if decision == "rn" and unit.get("doc_mentions"):
        return ["check_doc"]
    return []


def build_queue(units: List[Dict[str, Any]], decisions: Dict[str, str], only_kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Все задания одним списком: агент проходит их за один раз. Задание decide включает и правку,
    если агент решил, что она нужна, поэтому второго круга заданий нет."""
    items = []
    for u in units:
        for k in kinds_for(decisions.get(u["id"], "skip"), u):
            items.append({"id": f"{unit_dir(u['id'])}__{k}", "unit": u["id"], "kind": k})
    if any(decisions.get(u["id"]) in ("doc", "rn", "langs", "agent") for u in units):
        items.append({"id": "release_notes", "unit": None, "kind": "release_notes"})
    if only_kinds:
        items = [i for i in items if i["kind"] in only_kinds]
    return sorted(items, key=lambda i: (KIND_ORDER.index(i["kind"]), i["id"]))


# ---------------------------------------------------------------- источники задачи

DIGEST_LINES_PER_FILE = 60
DIGEST_LINES_TOTAL = 500
DIGEST_ORDER = {"source": 0, "i18n": 1, "style": 3}
DIGEST_SKIP = ("test", "build", "ci", "mocks", "changelog", "docs_internal", "types", "assets")


def build_digest(ds, u: Dict[str, Any]) -> str:
    """Сводка изменений задачи одним файлом: только добавленные и удаленные строки из патчей.

    Агенту не нужно открывать десятки файлов: сначала он читает сводку. Строки взяты из патчей дословно,
    поэтому на сводку можно ссылаться в подтверждениях.
    """
    data = ds.data_dir / "code"
    rel = [f for f in u["files"] if f.get("relevant") and f.get("patch") and f["category"] not in DIGEST_SKIP]
    rel.sort(key=lambda f: (DIGEST_ORDER.get(f["category"], 2), f["path"]))
    L = [f"# Изменения кода по задаче {u['id']}", "",
         "Только измененные строки: `+` добавлено, `-` удалено. Сначала код приложения, затем тексты интерфейса, "
         "затем стили. Файлы тестов и сборки не показаны.", ""]
    total, shown = 0, 0
    for f in rel:
        if total >= DIGEST_LINES_TOTAL:
            break
        lines = [x for x in (read_text(data / f["patch"]) or "").splitlines()
                 if x.startswith("@@") or ((x.startswith("+") or x.startswith("-")) and not x.startswith(("+++", "---")))]
        cut = lines[:min(DIGEST_LINES_PER_FILE, DIGEST_LINES_TOTAL - total)]
        note = "" if f["reachable_from_app"] else ", приложение POINT 2 этот файл не использует"
        L += [f"## {f['path']} [{f['category']}, {f['status']}{note}]", "", "```diff", *cut, "```"]
        if len(cut) < len(lines):
            L.append(f"Показано строк: {len(cut)} из {len(lines)}. Полностью: `patches/{Path(f['patch']).name}`.")
        L.append("")
        total += len(cut)
        shown += 1
    if shown < len(rel):
        L += [f"Не показано файлов: {len(rel) - shown} из {len(rel)}. Их патчи лежат в `patches/`.", ""]
    if not rel:
        L += ["У значимых записей этой задачи нет измененных файлов кода.", ""]
    return "\n".join(L)


def unit_sources(ds, u: Dict[str, Any], kind: str = "") -> Dict[str, Path]:
    """Источники задачи: путь в рабочей папке агента -> настоящий файл. Только то, что привязано к задаче.

    Первым идет сводка изменений, затем отдельные патчи. Полные файлы кода не копируются: читать их долго,
    а все изменения уже есть в патчах.
    """
    out: Dict[str, Path] = {}
    base = f"sources/{unit_dir(u['id'])}"
    data = ds.data_dir / "code"
    new = ds.slot(P.SLOT_CODE_NEW)
    digest = ds.data_dir / "agent_digest" / f"{unit_dir(u['id'])}.md"
    if not digest.is_file():
        save_text(digest, build_digest(ds, u))
    out[f"{base}/changes.md"] = digest
    for f in u["files"]:
        if f.get("relevant") and f.get("patch"):
            out[f"{base}/patches/{Path(f['patch']).name}"] = data / f["patch"]
    if kind == "check_langs":
        for p in LANG_SETTINGS:
            if (new / p).is_file():
                out[f"{base}/code/{p}"] = new / p
    return out


def unit_pages(ds, u: Dict[str, Any]) -> List[str]:
    """Страницы документации задачи в рабочей папке агента: кандидаты и места со старыми текстами."""
    pages = {c["doc"] for c in u["doc_candidates"]}
    pages |= {h["file"] for hits in (u.get("doc_mentions") or {}).values() for found in hits.values() for h in found}
    out = []
    for lang in ds.langs:
        for page in sorted(pages):
            if (ds.result_dir / f"MD_{lang}" / "docs" / page).is_file():
                out.append(f"docs/MD_{lang}/docs/{page}")
    return out


# ---------------------------------------------------------------- текст задания

def _unit_context(ds, u: Dict[str, Any], conf: Dict[str, Any], kind: str) -> str:
    L = [f"## Задача {u['id']}", "", f"Решение правил: {V.rule_text(u)}.", ""]
    flags = conf.get(u["id"], {}).get("flags", [])
    if flags:
        L += ["Основание для проверки:", ""] + [f"- {f['rule']}: {f['text']}" for f in flags] + [""]
    L += ["Записи списка изменений (учитываются только значимые):", ""]
    for e in u["entries"]:
        fl = e["_flags"]
        mark = "значимая" if fl.get("significant") else ("другой продукт" if fl["other_product"] else "тест" if fl["test"] else "техническая")
        L.append(f"- [{mark}] [{e['section']}] [{']['.join(e['tags'])}] {e['text']}")
    i = u["i18n"]
    if i["added"] or i["removed"] or i["changed"]:
        L += ["", "Тексты интерфейса:", ""]
        L += [f"- добавлен [{x['locale']}]: «{x['value']}»" for x in i["added"][:40]]
        L += [f"- удален [{x['locale']}]: «{x['value']}»" for x in i["removed"][:40]]
        L += [f"- изменен [{x['locale']}]: «{x['old']}» -> «{x['new']}»" for x in i["changed"][:40]]
    j = u.get("jira")
    if j:
        L += ["", f"Задача в Jira: {j.get('summary') or ''}", "", (j.get("description") or "")[:1500]]
    for lang, hits in (u.get("doc_mentions") or {}).items():
        for needle, found in hits.items():
            L.append(f"- старый текст «{needle}» в документации [{lang}]: "
                     + ", ".join(f"`docs/MD_{lang}/docs/{h['file']}`:{h['line']}" for h in found[:6]))
    pages = unit_pages(ds, u)
    L += ["", "## Страницы документации этой задачи", ""]
    L += [f"- `{p}`" for p in pages] or ["Страницы не определены: если без них задание не выполнить, верни `needs_human`."]
    L += ["", "## Разрешенные источники", "",
          "Открывай только эти файлы и страницы из списка выше. В подтверждениях указывай источник точно так, "
          "как он записан здесь.", "",
          "- `changelog` - записи списка изменений этой задачи (выше)",
          "- `i18n` - тексты интерфейса этой задачи (выше)"]
    if j:
        L.append("- `jira` - название и описание задачи в Jira (выше)")
    srcs = list(unit_sources(ds, u, kind))
    L.append(f"- `{srcs[0]}` - сводка всех изменений кода по задаче. Начни с нее: обычно ее достаточно")
    extra = [x for x in srcs[1:] if "/patches/" not in x]
    L += [f"- `{x}`" for x in extra]
    n = len(srcs) - 1 - len(extra)
    if n:
        L.append(f"- `sources/{unit_dir(u['id'])}/patches/` - полные патчи по файлам ({n}). Открывай отдельный патч, "
                 "только если в сводке строка обрезана или нужен контекст вокруг нее")
    L += ["", "## Где искать на страницах", ""]
    heads = _page_outline(ds, u)
    L += heads or ["Оглавления страниц нет."]
    first = (u.get("_round2") or {})
    if first:
        L += ["", "## Второй круг", "",
              "Это повторный разбор задания. Проверь первый ответ заново по источникам: подтверди его или исправь. "
              "Не опирайся на первый ответ как на источник.", "",
              f"Почему нужен второй круг: {first.get('reason') or 'не указано'}", "",
              "Первый ответ:", "", "```json", json.dumps(first.get("answer") or {}, ensure_ascii=False, indent=2)[:6000], "```"]
    return "\n".join(L)


def _page_outline(ds, u: Dict[str, Any]) -> List[str]:
    """Заголовки страниц задачи с номерами строк: агенту не нужно читать страницу целиком, чтобы найти раздел."""
    index_path = ds.data_dir / "06_docs_index.json"
    if not index_path.is_file():
        return []
    index = load_json(index_path)
    pages = {c["doc"] for c in u["doc_candidates"]}
    out = []
    for f in index.get("rus", {}).get("files", []):
        if f["path"] in pages:
            heads = [f"строка {h['line']}: {h['text']}" for h in f["headings"] if h["level"] <= 3][:25]
            out.append(f"- `docs/MD_rus/docs/{f['path']}` ({f['lines']} строк): " + "; ".join(heads))
    return out


def rules_text(ds) -> str:
    """Общие правила для агента (AGENTS.md его рабочей папки) без списка заданий."""
    return (read_text(PROMPTS / "workspace_agents.md") or "").replace("{{RELEASE}}", ds.versions["release"])


def rules_code(ds) -> str:
    """Код общих правил. Агент обязан повторить его в ответе: так видно, что правила он прочитал."""
    return "R-" + hashlib.sha256(rules_text(ds).replace("{{RULES_CODE}}", "").encode("utf-8")).hexdigest()[:8]


def render_task(ds, item: Dict[str, Any], units_by_id: Dict[str, Any], conf: Dict[str, Any], decisions: Dict[str, str]) -> str:
    """Задание: только данные конкретной задачи. Общие правила и порядок действий лежат в AGENTS.md."""
    v = ds.versions
    head = read_text(PROMPTS / "task.md") or ""
    if item["unit"]:
        ctx = _unit_context(ds, units_by_id[item["unit"]], conf, item["kind"])
    else:
        rows = [f"- {uid}: {V.DECISION_TEXT[d]}" for uid, d in decisions.items() if d in ("doc", "rn", "langs")]
        waiting = [f"- {uid}" for uid, d in decisions.items() if d == "agent"]
        rn = f"{P.RELEASE_NOTES_DIR}/{v['release_notes_file']}"
        prev = f"{P.RELEASE_NOTES_DIR}/{v['previous_release_notes_file']}"
        ctx = ("## Файлы\n\n" + "\n".join(f"- черновик: `docs/MD_{lang}/docs/{rn}`" for lang in ds.langs) + "\n"
               + "\n".join(f"- образец прошлого релиза: `docs/MD_{lang}/docs/{prev}`" for lang in ds.langs)
               + "\n\n## Состав строк\n\nСостав строк задает черновик, а не этот список: список только поясняет, "
                 "откуда какая строка.\n\n### Задачи с принятым решением: строка остается\n\n" + ("\n".join(rows) or "Нет.")
               + "\n\n### Задачи, решение по которым принимаешь ты в заданиях `decide`\n\n"
                 "В черновике у их строк стоит пометка «ожидает решения». Оставь строку, если в своем ответе по задаче "
                 "ты решил `doc`, `rn` или `langs`. Удали строку, если решил `skip` или `screens` либо вернул "
                 "`needs_human`. Спрашивать человека об этом не нужно.\n\n" + ("\n".join(waiting) or "Нет."))
    text = head.strip() + "\n\n" + ctx.strip() + "\n"
    for k, val in {"{{ITEM}}": item["id"], "{{KIND}}": item["kind"], "{{ANSWER_FILE}}": f"answers/{item['id']}.json"}.items():
        text = text.replace(k, val)
    return text


# ---------------------------------------------------------------- проверка ответа

def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _virtual_text(u: Dict[str, Any], source: str) -> str:
    if source == "changelog":
        return " \n ".join(e["text"] for e in u["entries"])
    if source == "i18n":
        i = u["i18n"]
        vals = [x["value"] for x in i["added"] + i["removed"]] + [y for c in i["changed"] for y in (c["old"], c["new"])]
        return " \n ".join(vals)
    j = u.get("jira") or {}
    return f"{j.get('summary') or ''} \n {j.get('description') or ''}"


def verify_evidence(ds, u: Dict[str, Any], kind: str, answer: Dict[str, Any]) -> List[str]:
    """Каждое утверждение агента должно быть подтверждено дословной цитатой из источников задачи.

    Цитаты из страниц документации сверяются с принятой копией (result/), а не с копией, которую правил агент.
    Возвращает список нарушений; пустой список - ответ подтвержден.
    """
    problems: List[str] = []
    sources = unit_sources(ds, u, kind)
    pages = {p: ds.result_dir / p[len("docs/"):] for p in unit_pages(ds, u)}

    def check(ev: Any, where: str) -> None:
        if not isinstance(ev, dict) or not ev.get("source") or not ev.get("quote"):
            problems.append(f"{where}: подтверждение должно быть объектом с полями source и quote")
            return
        src, quote = str(ev["source"]).strip("`"), _norm(str(ev["quote"]))
        if len(quote) < 3:
            problems.append(f"{where}: цитата слишком короткая")
            return
        if src in VIRTUAL_SOURCES:
            text = _virtual_text(u, src)
        elif src in sources:
            text = read_text(sources[src]) or ""
        elif src in pages:
            text = read_text(pages[src]) or ""
        else:
            problems.append(f"{where}: источник не входит в разрешенные для этой задачи: {src}")
            return
        if quote not in _norm(text):
            problems.append(f"{where}: цитаты нет в источнике {src}: «{quote[:80]}»")

    def check_list(evs: Any, where: str) -> None:
        if not isinstance(evs, list) or not evs:
            problems.append(f"{where}: нет подтверждения")
            return
        for n, ev in enumerate(evs, 1):
            check(ev, f"{where}, подтверждение {n}")

    steps = answer.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("шаги: нет списка steps - что открыл и что там нашел")
    else:
        for n, st in enumerate(steps, 1):
            src = str(st.get("source", "")).strip("`") if isinstance(st, dict) else ""
            if not src or not st.get("found"):
                problems.append(f"шаг {n}: нужны поля source и found")
            elif src not in VIRTUAL_SOURCES and src not in sources and src not in pages:
                problems.append(f"шаг {n}: открыт файл вне источников задачи: {src}")
    if kind == "decide":
        check_list(answer.get("evidence"), "решение")
    edits = answer.get("edits")
    changed = answer.get("files_changed") or []
    if kind in ("decide", "write_doc", "check_doc", "check_langs") and (changed or edits):
        if not isinstance(edits, list) or not edits:
            problems.append("правки: файлы изменены, но нет списка edits с подтверждением каждой правки")
        else:
            for n, e in enumerate(edits, 1):
                if not isinstance(e, dict) or not e.get("file") or not e.get("what"):
                    problems.append(f"правка {n}: нужны поля file и what")
                    continue
                check_list(e.get("evidence"), f"правка {n} ({e['file']})")
            problems += verify_pages(ds, u, answer, pages)
    return problems


QUOTED_RE = re.compile(r"«([^»\n]{2,80})»|\"([^\"\n]{2,80})\"|“([^”\n]{2,80})”")
LINES_PER_EDIT = 12


def _ui_vocabulary(ds) -> Dict[str, set]:
    """Все тексты интерфейса новой версии по языкам документации (кэшируется на время запуска)."""
    if not hasattr(ds, "_vocab"):
        lang_of = ds.rules["i18n"]["doc_lang_of_locale"]
        vocab: Dict[str, set] = {lang: set() for lang in lang_of.values()}
        rows = load_json(ds.data_dir / "03_i18n_rows.json")["new"]
        for r in rows:
            lang = lang_of.get(r["locale"])
            if lang:
                vocab[lang].add(_norm(r["value"]))
        ds._vocab = vocab
    return ds._vocab


def verify_pages(ds, u: Dict[str, Any], answer: Dict[str, Any], pages: Dict[str, Path]) -> List[str]:
    """Сверяет сами правки с реальными данными: размер правки и названия элементов интерфейса.

    * Правка минимальная: на одну заявленную правку не больше LINES_PER_EDIT измененных строк.
    * Каждое новое название в кавычках должно существовать: в текстах интерфейса этого языка,
      в прежнем тексте страницы или в записях задачи. Выдуманное название - нарушение.
    """
    problems: List[str] = []
    w = workspace(ds)
    known = " \n ".join(_virtual_text(u, k) for k in VIRTUAL_SOURCES)
    edits_per_file: Dict[str, int] = {}
    for e in answer.get("edits") or []:
        if isinstance(e, dict) and e.get("file"):
            f = str(e["file"]).strip("`")
            edits_per_file[f] = edits_per_file.get(f, 0) + 1
    for f, n_edits in sorted(edits_per_file.items()):
        if f in LOG_FILES or f not in pages:
            continue
        old = (read_text(pages[f]) or "").splitlines()
        new_text = read_text(w / f)
        if new_text is None:
            problems.append(f"{f}: файл назван измененным, но его нет")
            continue
        new = new_text.splitlines()
        diff = [d for d in difflib.ndiff(old, new) if d[:2] in ("+ ", "- ")]
        added = [d[2:] for d in diff if d.startswith("+ ")]
        if not diff:
            problems.append(f"{f}: правка заявлена, но файл не изменен")
            continue
        if len(diff) > LINES_PER_EDIT * n_edits:
            problems.append(f"{f}: изменено строк: {len(diff)}, заявлено правок: {n_edits}. Правка не минимальная "
                            "или не все правки названы")
        m = re.search(r"MD_(\w+)/", f)
        vocab = _ui_vocabulary(ds).get(m.group(1) if m else "", set())
        old_text = _norm("\n".join(old))
        for line in added:
            if line.startswith("!!!") or line.startswith("    "):
                continue                      # метка скриншота
            for t in QUOTED_RE.findall(line):
                term = _norm(next(x for x in t if x))
                if term in vocab or term in old_text or term in _norm(known):
                    continue
                problems.append(f"{f}: названия «{term}» нет ни в текстах интерфейса, ни на странице, ни в записях задачи")
    return problems


def verify_release_notes(ds, final: Dict[str, str]) -> List[str]:
    """Агент обязан переформулировать черновик, не меняя его состав: те же строки, та же таблица версий."""
    problems: List[str] = []
    w = workspace(ds)
    counts = {}
    for lang in ds.langs:
        rel = Path(f"MD_{lang}") / "docs" / P.RELEASE_NOTES_DIR / ds.versions["release_notes_file"]
        draft, new = read_text(ds.result_dir / rel), read_text(w / "docs" / rel)
        if draft is None or DRAFT.DRAFT_MARK not in draft:
            continue                              # черновика уже нет: текст принят раньше
        if new is None:
            problems.append(f"[{lang}] файла release notes нет")
            continue
        if DRAFT.DRAFT_MARK in new or "<!--" in new:
            problems.append(f"[{lang}] в файле остались служебные комментарии или метка черновика")
        expected = 0
        for line in draft.splitlines():
            if not line.startswith("- **["):
                continue
            m = re.search(r"<!-- (\S+?),", line)
            uid = m.group(1) if m else None
            if DRAFT.PENDING_NOTE in line and final.get(uid) not in ("doc", "rn", "langs"):
                continue                          # задача отброшена: строки быть не должно
            expected += 1
        got = sum(1 for line in new.splitlines() if line.startswith("- **["))
        counts[lang] = got
        if got != expected:
            problems.append(f"[{lang}] строк в release notes: {got}, должно быть: {expected} (по одной на принятую задачу)")
        if [l for l in draft.splitlines() if l.startswith("|")] != [l for l in new.splitlines() if l.startswith("|")]:
            problems.append(f"[{lang}] изменена таблица версий компонентов")
        if [l for l in draft.splitlines() if l.startswith("# ")] != [l for l in new.splitlines() if l.startswith("# ")]:
            problems.append(f"[{lang}] изменен заголовок с версией и датой")
    if len(set(counts.values())) > 1:
        problems.append(f"в языках разное число строк: {counts}")
    return problems


def check_answer(path: Path, item: Dict[str, Any], ds=None, unit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = read_text(path)
    if raw is None:
        return {"ok": False, "missing": True, "error": "ответа нет"}
    try:
        a = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"ответ не является JSON: {e}"}
    if not isinstance(a, dict):
        return {"ok": False, "error": "ответ должен быть объектом JSON"}
    miss = [k for k in ANSWER_REQUIRED if k not in a]
    if miss:
        return {"ok": False, "error": "в ответе нет полей: " + ", ".join(miss), "answer": a}
    if a["status"] not in STATUSES:
        return {"ok": False, "error": f"неизвестный status: {a['status']}", "answer": a}
    if ds is not None and str(a.get("rules", "")).strip() != rules_code(ds):
        return {"ok": False, "unverified": True, "answer": a,
                "problems": ["в ответе нет кода общих правил: агент не прочитал AGENTS.md в своей папке"],
                "error": "агент не прочитал общие правила (нет кода из AGENTS.md)"}
    if a["status"] == "recheck" and not str(a.get("recheck_reason") or "").strip():
        return {"ok": False, "error": "для статуса recheck нужно поле recheck_reason: в чем сомнение", "answer": a}
    if item["kind"] == "decide" and a["status"] in FINAL and a.get("decision") not in ("doc", "rn", "langs", "screens", "skip"):
        return {"ok": False, "error": "для задания decide нужно поле decision: doc, rn, langs, screens или skip", "answer": a}
    if ds is not None and unit is not None and item["kind"] in NEED_EVIDENCE and a["status"] in FINAL:
        problems = verify_evidence(ds, unit, item["kind"], a)
        if problems:
            return {"ok": False, "unverified": True, "problems": problems, "answer": a,
                    "error": "не подтверждено источниками: " + "; ".join(problems[:4])
                             + (f"; и еще нарушений: {len(problems) - 4}" if len(problems) > 4 else "")}
    return {"ok": True, "answer": a}


def _declared_files(answer: Dict[str, Any]) -> List[str]:
    files = [str(x).strip("`") for x in (answer.get("files_changed") or [])]
    files += [str(e.get("file", "")).strip("`") for e in (answer.get("edits") or []) if isinstance(e, dict)]
    return sorted({f for f in files if f})


# ---------------------------------------------------------------- рабочая папка агента

def _doc_files(root: Path) -> Dict[str, Path]:
    out = {}
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.name != ".DS_Store":
                out[p.relative_to(root).as_posix()] = p
    return out


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def changed_in_workspace(ds) -> List[str]:
    """Файлы документации, которые агент изменил в своей рабочей папке с момента, когда пайплайн ее собрал.

    Сравнение идет со списком контрольных сумм, записанным при сборке папки, а не с result/: копию в result/
    пайплайн сам обновляет между запусками (например, пересобирает черновик release notes).
    """
    manifest_path = ds.agent_dir / "workspace_manifest.json"
    if not manifest_path.is_file():
        return []
    manifest = load_json(manifest_path)
    w = _doc_files(workspace(ds) / "docs")
    return [f"docs/{k}" for k, p in w.items() if manifest.get(k) != _digest(p)]


def refresh_workspace(ds, items: List[Dict[str, Any]], prompts: Dict[str, str], units_by_id: Dict[str, Any]) -> None:
    """Собирает рабочую папку агента заново: принятая документация, задания, источники задач. Ответы сохраняются."""
    w = workspace(ds)
    for sub in ("docs", "tasks", "sources"):
        if (w / sub).exists():
            shutil.rmtree(w / sub)
    shutil.copytree(ds.result_dir, w / "docs", ignore=shutil.ignore_patterns(".DS_Store"))
    (w / "answers").mkdir(parents=True, exist_ok=True)
    if not (w / "progress.md").exists():
        save_text(w / "progress.md", "# Ход работы агента\n\nАгент дописывает сюда строку в начале и в конце каждого задания и на каждом шаге.\n\n")
    for item in items:
        save_text(w / "tasks" / f"{item['id']}.md", prompts[item["id"]])
        if item["unit"]:
            for dst, src in unit_sources(ds, units_by_id[item["unit"]], item["kind"]).items():
                t = w / dst
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, t)
    save_json(ds.agent_dir / "workspace_manifest.json", {k: _digest(p) for k, p in _doc_files(w / "docs").items()})
    order = "\n".join(f"{n}. `tasks/{i['id']}.md`" for n, i in enumerate(items, 1)) or "Заданий нет."
    text = rules_text(ds).replace("{{RULES_CODE}}", rules_code(ds)).replace("{{TASKS}}", order)
    save_text(w / "AGENTS.md", text)


def accept_edits(ds, results: Dict[str, Dict[str, Any]], allowed: Dict[str, List[str]]) -> Dict[str, Any]:
    """Переносит в result/ правки из подтвержденных ответов. Остальные изменения откладывает в agent/rejected/.

    allowed - какие страницы разрешено менять каждому заданию.
    """
    w = workspace(ds)
    changed = changed_in_workspace(ds)
    good: Dict[str, List[str]] = {}
    bad: Dict[str, List[str]] = {}
    foreign: Dict[str, List[str]] = {}
    for iid, r in results.items():
        a = r.get("answer") or {}
        for f in _declared_files(a):
            if f in LOG_FILES:
                continue
            if not r["ok"]:
                bad.setdefault(f, []).append(iid)
            elif f not in allowed.get(iid, []):
                foreign.setdefault(f, []).append(iid)
            else:
                good.setdefault(f, []).append(iid)
    any_ok = any(r["ok"] for r in results.values())
    accepted, rejected = [], []
    for f in changed:
        src = w / f
        if f in LOG_FILES:
            ok, why = any_ok, "журнал: нет ни одного принятого ответа"
        elif f in bad:
            ok, why = False, "файл назван в неподтвержденном ответе: " + ", ".join(bad[f])
        elif f in foreign:
            ok, why = False, "страница не входит в страницы задания: " + ", ".join(foreign[f])
        elif f in good:
            ok, why = True, ""
        else:
            ok, why = False, "файл изменен, но ни в одном ответе не назван"
        if ok:
            dst = ds.result_dir / f[len("docs/"):]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            accepted.append({"file": f, "by": good.get(f, [])})
        else:
            dst = ds.agent_dir / "rejected" / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            rejected.append({"file": f, "why": why})
    return {"accepted": accepted, "rejected": rejected}


# ---------------------------------------------------------------- запуск

def run_cmd(ds, item: Dict[str, Any], prompt: str, cmd: List[str], timeout: int) -> Optional[str]:
    """Запускает агента в его рабочей папке. Возвращает текст ошибки или None."""
    try:
        res = subprocess.run(cmd, input=prompt, text=True, capture_output=True, cwd=str(workspace(ds)), timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"агент не уложился в {timeout} с"
    except FileNotFoundError:
        return f"команда агента не найдена: {cmd[0]}"
    save_text(ds.agent_dir / "logs" / f"{item['id']}.log", (res.stdout or "") + ("\n--- stderr ---\n" + res.stderr if res.stderr else ""))
    if res.returncode != 0:
        return f"агент завершился с кодом {res.returncode}, см. logs/{item['id']}.log"
    return None


def run(ds, units: List[Dict[str, Any]], conf: Dict[str, Any], decisions: Dict[str, str], *, dry_run: bool,
        cmd: Optional[str], redo: bool, timeout: int, recheck: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Все задания готовятся сразу. Подтвержденные ответы decide дополняют решения человека."""
    by_id = {u["id"]: u for u in units}
    command = agent_cmd(cmd)
    launch = bool(command) and not dry_run
    w = workspace(ds)
    final = dict(decisions)
    by_round2: Dict[str, Dict[str, Any]] = {}
    results: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}
    launched_seconds: Dict[str, int] = {}

    qpath = ds.agent_dir / "queue.json"
    prev = {i["id"]: i for i in (load_json(qpath)["items"] if qpath.is_file() else [])}

    def check(item) -> Dict[str, Any]:
        path = w / "answers" / f"{item['id']}.json"
        old = prev.get(item["id"], {})
        if path.is_file() and old.get("status") in STATUSES and old.get("answer_sha") == _digest(path):
            # ответ уже проверен и принят в прошлый запуск: его правки перенесены, повторно не сверяются
            r = {"ok": True, "answer": json.loads(read_text(path) or "{}"), "kept": True}
        else:
            r = check_answer(path, item, ds, by_id.get(item["unit"]) if item["unit"] else None)
        results[item["id"]] = r
        return r

    def process(items: List[Dict[str, Any]]) -> None:
        prompts = {i["id"]: render_task(ds, i, {**by_id, **({i["unit"]: by_round2[i["id"]]} if i["id"] in by_round2 else {})},
                                        conf, final) for i in items}
        if launch:
            earlier.append(sync_now())      # правки, сделанные до пересборки папки, не теряются
            refresh_workspace(ds, all_items + [i for i in items if i not in all_items], {**all_prompts, **prompts}, by_id)
        for n, item in enumerate(items, 1):
            all_prompts[item["id"]] = prompts[item["id"]]
            all_items.append(item)
            answer = w / "answers" / f"{item['id']}.json"
            if redo and launch and answer.exists():
                answer.unlink()
            r = check(item)
            if launch and not r["ok"]:
                log(f"агент: {item['id']} ({n}/{len(items)})")
                if answer.exists():
                    answer.unlink()
                t0 = time.time()
                err = run_cmd(ds, item, prompts[item["id"]], command, timeout)
                launched_seconds[item["id"]] = int(time.time() - t0)
                log(f"агент: {item['id']} - {launched_seconds[item['id']]} с")
                if err:
                    errors[item["id"]] = err
                r = check(item)
            if item["kind"] == "decide" and r["ok"] and r["answer"]["status"] in FINAL:
                final[item["unit"]] = r["answer"]["decision"]

    all_items: List[Dict[str, Any]] = []
    all_prompts: Dict[str, str] = {}
    earlier: List[Dict[str, Any]] = []
    rn_files = [f"docs/MD_{lang}/docs/{P.RELEASE_NOTES_DIR}/{ds.versions['release_notes_file']}" for lang in ds.langs]

    def sync_now() -> Dict[str, Any]:
        allowed = {i["id"]: (unit_pages(ds, by_id[i["unit"]]) if i["unit"] else rn_files) for i in all_items}
        return accept_edits(ds, results, allowed)

    queue = build_queue(units, decisions)
    # второй круг: задания, которые агент сам отметил в прошлый запуск, и те, что назвал человек (--recheck)
    rpath = ds.agent_dir / "recheck.json"
    wanted: Dict[str, str] = load_json(rpath) if rpath.is_file() else {}
    for i in queue:
        old = prev.get(i["id"], {})
        if old.get("status") == "recheck":
            wanted.setdefault(i["id"], "агент: " + (old.get("recheck_reason") or "сомнение не описано"))
        for key, note in (recheck or {}).items():
            if key in (i["id"], i["unit"]):
                wanted[i["id"]] = "человек: " + (note or "проверить еще раз")
    save_json(rpath, wanted)
    round2 = []
    for i in queue:
        if i["id"] in wanted and i["unit"]:
            first = w / "answers" / f"{i['id']}.json"
            unit2 = dict(by_id[i["unit"]])
            unit2["_round2"] = {"reason": wanted[i["id"]], "answer": json.loads(read_text(first) or "{}") if first.is_file() else {}}
            by_round2[i["id"] + "__round2"] = unit2
            round2.append({"id": i["id"] + "__round2", "unit": i["unit"], "kind": i["kind"], "round": 2, "first": i["id"]})
    process(queue + round2)
    for i in round2:                       # второй круг заменяет решение первого; третьего круга нет
        r = results[i["id"]]
        if r["ok"] and r["answer"]["status"] == "recheck":
            r["answer"]["status"] = "needs_human"
        if i["kind"] == "decide" and r["ok"] and r["answer"]["status"] == "done":
            final[i["unit"]] = r["answer"]["decision"]
    # release notes проверяются по итоговым решениям: строки отброшенных задач должны быть убраны
    rn_item = next((i for i in all_items if i["kind"] == "release_notes"), None)
    if rn_item and results[rn_item["id"]]["ok"] and not results[rn_item["id"]].get("kept") \
            and results[rn_item["id"]]["answer"]["status"] in FINAL:
        left = [i["id"] for i in all_items if i["kind"] == "decide" and not results[i["id"]]["ok"]]
        probs = ([f"не завершены задания, от которых зависит состав строк: {', '.join(left)}"] if left
                 else verify_release_notes(ds, final))
        if probs:
            results[rn_item["id"]] = {"ok": False, "unverified": True, "problems": probs,
                                      "answer": results[rn_item["id"]]["answer"],
                                      "error": "не подтверждено: " + "; ".join(probs[:4])}

    # время по заданиям: при ручном запуске оценивается по времени записи файлов ответов
    timing: Dict[str, int] = {i: v for i, v in launched_seconds.items()}
    if not launch:
        start = (ds.agent_dir / "workspace_manifest.json").stat().st_mtime if (ds.agent_dir / "workspace_manifest.json").is_file() else None
        done_at = sorted((p.stat().st_mtime, p.stem) for p in (w / "answers").glob("*.json"))
        for t, name in done_at:
            if start is not None and t >= start:
                timing[name] = int(t - start)
            start = t
        for i in all_items:                 # для ответов, принятых раньше, время берется из прошлого журнала
            if results[i["id"]].get("kept") and prev.get(i["id"], {}).get("seconds") is not None:
                timing[i["id"]] = prev[i["id"]]["seconds"]
    if (w / "progress.md").is_file():
        shutil.copyfile(w / "progress.md", ds.agent_dir / "progress.md")
    sync = sync_now()
    for e in earlier:
        sync["accepted"] = e["accepted"] + sync["accepted"]
        sync["rejected"] = e["rejected"] + sync["rejected"]
    refresh_workspace(ds, all_items, all_prompts, by_id)

    for item in all_items:
        r = results[item["id"]]
        a = r.get("answer") or {}
        if item["id"] in errors and not r["ok"]:
            item.update(status="failed", note=errors[item["id"]])
        elif r["ok"]:
            item.update(status=a["status"], note=str(a.get("summary", ""))[:300])
        elif r.get("missing"):
            item.update(status="prepared", note="ответа нет")
        elif r.get("unverified"):
            item.update(status="unverified", note=r["error"])
        else:
            item.update(status="failed", note=r["error"])
        apath = w / "answers" / f"{item['id']}.json"
        item["answer_sha"] = _digest(apath) if apath.is_file() else None
        item["recheck_reason"] = str(a.get("recheck_reason") or "") if r["ok"] and a.get("status") == "recheck" else ""
        item["seconds"] = timing.get(item["id"])
        item["decision"] = a.get("decision") if item["kind"] == "decide" and r["ok"] else None
        item["problems"] = r.get("problems", [])
        item["log"] = [f"{st.get('source')}: {st.get('found')}" for st in (a.get("steps") or []) if isinstance(st, dict)][:30]
        item["declared_files"] = _declared_files(a)
        item["questions"] = [str(x) for x in (a.get("questions") or [])]
    save_json(ds.agent_dir / "queue.json", {"command": command, "launched": launch, "items": all_items, **sync})
    save_json(ds.agent_dir / "final_decisions.json", final)
    return {"items": all_items, "final": final, "launched": launch, "command": command, **sync}


def summary_md(ds, res: Dict[str, Any], decisions: Dict[str, str]) -> str:
    w = ds.rel(workspace(ds))
    counts: Dict[str, int] = {}
    for i in res["items"]:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    names = {"done": "принято", "recheck": "принято, нужен второй круг", "needs_human": "нужен человек", "unverified": "не подтверждено",
             "failed": "ошибка", "prepared": "ждет агента"}
    L = [f"# Журнал работы агента: набор данных {ds.name}", "",
         "Состояние заданий: " + (", ".join(f"{names[k]} - {v}" for k, v in sorted(counts.items())) or "заданий нет") + ".", ""]
    if not res["launched"]:
        L += [f"Агент запускается вручную. Откройте в редакторе только папку `{w}` и дайте агенту задания из "
              "`tasks/` по порядку из файла `AGENTS.md`. После этого запустите команду `agent` еще раз: она проверит "
              "ответы, перенесет принятые правки и обновит этот журнал.", ""]
    total = sum(i.get("seconds") or 0 for i in res["items"])
    if total:
        L += [f"Время агента: {total // 60} мин {total % 60} с" + ("" if res["launched"] else " (оценка по времени записи ответов)")
              + ". Ход работы по шагам: `agent/progress.md`.", ""]
    again = [i for i in res["items"] if i["status"] == "recheck"]
    if again:
        L += ["**Агент просит второй круг** по заданиям: " + ", ".join(i["id"] for i in again)
              + ". Проверьте их ниже и запустите команду `agent` еще раз: для них будут подготовлены задания второго круга.", ""]
    L += ["| Задание | Состояние | Решение | Время | Итог |", "| --- | --- | --- | --- | --- |"]
    for i in res["items"]:
        sec = i.get("seconds")
        t = f"{sec // 60}:{sec % 60:02d}" if sec is not None else "-"
        L.append(f"| {i['id']} | {names[i['status']]} | {i.get('decision') or '-'} | {t} | {(i.get('note') or '-').replace('|', '/')} |")
    if res["accepted"]:
        L += ["", "## Принятые правки", "", "Перенесены в `result/`.", ""]
        L += [f"- `{a['file']}`" + (f": {', '.join(a['by'])}" if a["by"] else "") for a in res["accepted"]]
    if res["rejected"]:
        L += ["", "## Отклоненные правки", "",
              "В `result/` не перенесены. Копии лежат в `agent/rejected/`, в рабочей папке агента файлы возвращены "
              "к принятому состоянию.", ""]
        L += [f"- `{r['file']}`: {r['why']}" for r in res["rejected"]]
    detail = [i for i in res["items"] if i["problems"] or i["log"] or i["questions"] or i["status"] in ("failed", "recheck")]
    if detail:
        L += ["", "## Подробности по заданиям", ""]
    for i in detail:
        L += [f"### {i['id']}: {names[i['status']]}", ""]
        if i.get("recheck_reason"):
            L += [f"Сомнение агента: {i['recheck_reason']}", ""]
        if i["problems"]:
            L += ["Что не подтверждено:", ""] + [f"- {p}" for p in i["problems"]] + [""]
        if i["questions"]:
            L += ["Вопросы агента:", ""] + [f"- {q}" for q in i["questions"]] + [""]
        if i["log"]:
            L += ["Шаги агента (источник: что нашел):", ""] + [f"- {x}" for x in i["log"]] + [""]
        if i["declared_files"]:
            L += ["Файлы, которые агент назвал измененными: " + ", ".join(f"`{f}`" for f in i["declared_files"]), ""]
    changed = {k: v for k, v in res["final"].items() if decisions.get(k) != v}
    if changed:
        L += ["", "## Решения агента", ""] + [f"- {k}: {V.DECISION_TEXT[v]}" for k, v in changed.items()]
    left = [k for k, v in res["final"].items() if v == "agent"]
    if left:
        L += ["", "Остались без решения: " + ", ".join(left) + "."]
    return "\n".join(L) + "\n"
