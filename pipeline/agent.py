"""Обертка агента: изолированная рабочая папка, задания, проверка ответов, внесение правок, журнал.

Агент ничего не меняет в документации сам. Он возвращает правки как данные (файл, точный существующий
фрагмент, новый текст), а для release notes - формулировки по задачам. Код проверяет каждую правку и сам
вносит ее, сам ведет trace.md и screenshots.md и сам собирает файлы release notes.


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
MAX_EDIT_LINES = 30          # строк нового текста в одной правке
MAX_FIND_CHARS = 2000
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
         "затем стили. Файлы тестов и сборки не показаны.", "",
         "У каждого файла указано, использует ли его приложение POINT 2: это установлено по связям между файлами "
         "кода, перепроверять не нужно.", ""]
    total, shown = 0, 0
    for f in rel:
        if total >= DIGEST_LINES_TOTAL:
            break
        lines = [x for x in (read_text(data / f["patch"]) or "").splitlines()
                 if x.startswith("@@") or ((x.startswith("+") or x.startswith("-")) and not x.startswith(("+++", "---")))]
        cut = lines[:min(DIGEST_LINES_PER_FILE, DIGEST_LINES_TOTAL - total)]
        other = any(m in f["path"] for m in ds.rules["product"]["other_product_file_markers"])
        note = (", файл другого продукта" if other else
                ", используется приложением POINT 2" if f["reachable_from_app"] else ", приложение POINT 2 этот файл не использует")
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
    L += ["", "## Страницы документации этой задачи", "", "Правки можно предлагать только для этих страниц.", ""]
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
        L = ["## Задачи для release notes", "",
             "Для каждой задачи дай одну формулировку на русском и одну на английском. Источник - только записи "
             "этой задачи ниже и то, что ты сам установил по ней в заданиях этого прохода.", ""]
        prev = f"docs/MD_rus/docs/{P.RELEASE_NOTES_DIR}/{v['previous_release_notes_file']}"
        L += [f"Образец стиля: `{prev}` и тот же файл в `docs/MD_eng/`.", ""]
        for uid, d in decisions.items():
            if d not in ("doc", "rn", "langs", "agent"):
                continue
            u = units_by_id[uid]
            src = DRAFT.rn_source(u)
            state = {"doc": "изменение", "langs": "изменение: набор языков", "rn": "исправление ошибки",
                     "agent": "решение принимаешь ты в задании decide: давай строку, только если решил doc, rn или langs"}[d]
            L += [f"### {uid} ({state})", ""]
            if src["jira"]:
                L.append(f"- название задачи (внутреннее, не для пользователя): {src['jira']}")
            L += [f"- запись разработчиков: {t}" for t in src["texts"][:10]]
            L.append("")
        ctx = "\n".join(L)
    text = head.strip() + "\n\n" + ctx.strip() + "\n"
    for k, val in {"{{ITEM}}": item["id"], "{{KIND}}": item["kind"], "{{ANSWER_FILE}}": f"answers/{item['id']}.json"}.items():
        text = text.replace(k, val)
    return text


# ---------------------------------------------------------------- проверка ответа

QUOTED_RE = re.compile(r"«([^»\n]{2,80})»|\"([^\"\n]{2,80})\"|“([^”\n]{2,80})”")


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _norm_code(text: str) -> str:
    """Текст патча или сводки без знаков '+' и '-' в начале строк: агент цитирует код без них."""
    return _norm("\n".join(re.sub(r"^[+-](?![+-])", "", line) for line in (text or "").splitlines()))


def _own_files(item_id: str) -> tuple:
    """Служебные файлы, которые агент открывает по ходу работы: это не источники, но и не нарушение."""
    return (f"tasks/{item_id}.md", "AGENTS.md", "progress.md")


def _virtual_text(u: Dict[str, Any], source: str) -> str:
    """Текст виртуального источника. Записи списка изменений даются и в том виде, как они напечатаны в задании."""
    if source == "changelog":
        rows = []
        for e in u["entries"]:
            rows.append(e["text"])
            rows.append(f"[{e['section']}] [{']['.join(e['tags'])}] {e['text']}")
        return " \n ".join(rows)
    if source == "i18n":
        i = u["i18n"]
        vals = [x["value"] for x in i["added"] + i["removed"]] + [y for c in i["changed"] for y in (c["old"], c["new"])]
        return " \n ".join(vals)
    j = u.get("jira") or {}
    return f"{j.get('summary') or ''} \n {j.get('description') or ''}"


def _ui_vocabulary(ds) -> Dict[str, set]:
    """Все тексты интерфейса новой версии по языкам документации (кэшируется на время запуска)."""
    if not hasattr(ds, "_vocab"):
        lang_of = ds.rules["i18n"]["doc_lang_of_locale"]
        vocab: Dict[str, set] = {lang: set() for lang in lang_of.values()}
        for r in load_json(ds.data_dir / "03_i18n_rows.json")["new"]:
            lang = lang_of.get(r["locale"])
            if lang:
                vocab[lang].add(_norm(r["value"]))
        ds._vocab = vocab
    return ds._vocab


class Docs:
    """Принятая копия документации (result/) в памяти: правки проверяются и вносятся по одной."""

    def __init__(self, ds):
        self.ds = ds
        self.cache: Dict[str, Optional[str]] = {}
        self.dirty: set = set()

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


def verify_answer(ds, u: Dict[str, Any], kind: str, answer: Dict[str, Any], docs: Docs, conf: Optional[Dict[str, Any]] = None,
                  item_id: str = "") -> Dict[str, Any]:
    """Сверяет ответ с реальными данными. Возвращает нарушения и готовые к внесению правки.

    * каждое утверждение и каждая правка подтверждены дословной цитатой из источников задачи;
    * шаги ссылаются только на источники и страницы задачи;
    * правка: страница входит в страницы задачи, заменяемый фрагмент есть на ней ровно один раз,
      новый текст не длиннее MAX_EDIT_LINES строк, новые названия в кавычках существуют в интерфейсе.
    Правки проверяются по порядку на копии страницы: вторая правка видит результат первой.
    """
    problems: List[str] = []
    sources = unit_sources(ds, u, kind)
    pages = unit_pages(ds, u)
    known = _norm(" \n ".join(_virtual_text(u, k) for k in VIRTUAL_SOURCES))
    # агент цитирует записи, тексты интерфейса и Jira в том виде, как они напечатаны в задании
    printed = _norm(_unit_context(ds, u, conf or {}, kind))

    def in_source(src: str, quote: str) -> Optional[bool]:
        """True - цитата есть; False - нет; None - источник не разрешен. Папка источников ищется целиком."""
        if src in VIRTUAL_SOURCES:
            return quote in _norm(_virtual_text(u, src)) or quote in printed
        if src in sources:
            text = read_text(sources[src]) or ""
            return quote in _norm(text) or _norm_code(quote) in _norm_code(text)
        if src in pages:
            return quote in _norm(read_text(docs.path(src)) or "")
        inside = [p for k, p in sources.items() if k.startswith(src.rstrip("/") + "/")]
        if inside:
            return any(_norm_code(quote) in _norm_code(read_text(p) or "") for p in inside)
        return None

    def check_ev(ev: Any, where: str) -> None:
        if not isinstance(ev, dict) or not ev.get("source") or not ev.get("quote"):
            problems.append(f"{where}: подтверждение должно быть объектом с полями source и quote")
            return
        src, quote = str(ev["source"]).strip("`"), _norm(str(ev["quote"]))
        if len(quote) < 3:
            problems.append(f"{where}: цитата слишком короткая")
            return
        found = in_source(src, quote)
        if found is None:
            problems.append(f"{where}: источник не входит в разрешенные для этой задачи: {src}")
        elif not found:
            problems.append(f"{where}: цитаты нет в источнике {src}: «{quote[:80]}»")

    def check_list(evs: Any, where: str) -> None:
        if not isinstance(evs, list) or not evs:
            problems.append(f"{where}: нет подтверждения")
            return
        for n, ev in enumerate(evs, 1):
            check_ev(ev, f"{where}, подтверждение {n}")

    steps = answer.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("шаги: нет списка steps - что открыл и что там нашел")
    else:
        for n, st in enumerate(steps, 1):
            src = str(st.get("source", "")).strip("`") if isinstance(st, dict) else ""
            if not src or not st.get("found"):
                problems.append(f"шаг {n}: нужны поля source и found")
            elif src not in VIRTUAL_SOURCES and src not in sources and src not in pages and src not in _own_files(item_id) \
                    and not any(k.startswith(src.rstrip("/") + "/") for k in sources):
                problems.append(f"шаг {n}: открыт файл вне источников задачи: {src}")
    if kind == "decide":
        check_list(answer.get("evidence"), "решение")

    ops: List[Dict[str, Any]] = []
    staged: Dict[str, str] = {}
    edits = answer.get("edits") or []
    if not isinstance(edits, list):
        problems.append("edits должен быть списком")
        edits = []
    for n, e in enumerate(edits, 1):
        where = f"правка {n}"
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) and e.get(k) for k in ("file", "find", "what")) \
                or not isinstance(e.get("replace"), str):
            problems.append(f"{where}: нужны поля file, find, replace, what")
            continue
        f = e["file"].strip("`")
        where = f"правка {n} ({f})"
        if f not in pages:
            problems.append(f"{where}: страница не входит в страницы задания")
            continue
        text = staged.get(f, docs.text(f))
        if text is None:
            problems.append(f"{where}: страницы нет")
            continue
        find, new = e["find"], e["replace"]
        count = text.count(find)
        if count == 0:
            problems.append(f"{where}: фрагмента find нет на странице дословно: «{_norm(find)[:70]}»")
            continue
        if count > 1:
            problems.append(f"{where}: фрагмент find встречается на странице {count} раза: возьми более длинный фрагмент")
            continue
        if find == new:
            problems.append(f"{where}: новый текст совпадает со старым")
            continue
        if len(find) > MAX_FIND_CHARS or len(new.splitlines()) > MAX_EDIT_LINES:
            problems.append(f"{where}: правка слишком большая (не больше {MAX_EDIT_LINES} строк нового текста): раздели на несколько")
            continue
        if "<!--" in new and "<!--" not in find:
            problems.append(f"{where}: в новом тексте комментарий: вопросы и пояснения пишутся в ответе, а не в документации")
        m = re.search(r"MD_(\w+)/", f)
        vocab = _ui_vocabulary(ds).get(m.group(1) if m else "", set())
        page_norm = _norm(text)
        for line in new.splitlines():
            if line.startswith("!!!") or line.startswith("    "):
                continue                                  # метка скриншота
            for t in QUOTED_RE.findall(line):
                term = _norm(next(x for x in t if x))
                if term in vocab or term in page_norm or term in known:
                    continue
                problems.append(f"{where}: названия «{term}» нет ни в текстах интерфейса новой версии, ни на странице, "
                                "ни в записях задачи")
        check_list(e.get("evidence"), where)
        staged[f] = text.replace(find, new, 1)
        ops.append({"file": f, "find": find, "replace": new, "what": e["what"],
                    "screenshot": "SCREENSHOT-TODO" in new and "SCREENSHOT-TODO" not in find})
    return {"problems": problems, "ops": ops, "staged": staged}


def apply_ops(ds, docs: Docs, uid: str, verified: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Вносит проверенные правки в result/ и сам записывает их в trace.md и screenshots.md."""
    for f, text in verified["staged"].items():
        docs.put(f, text)
    docs.save()
    trace = ds.result_dir / "trace.md"
    shots = ds.result_dir / "screenshots.md"
    t_text, s_text = read_text(trace) or "", read_text(shots) or ""
    for op in verified["ops"]:
        m = re.match(r"docs/MD_(\w+)/docs/(.+)$", op["file"])
        lang, page = (m.group(1), m.group(2)) if m else ("", op["file"])
        t_text += f"| {uid} | {lang} | {page} | {op['what'].replace('|', '/')} | агент, проверено кодом |\n"
        if op["screenshot"]:
            img = re.search(r"!\[[^\]]*\]\(([^)\s]+)", op["replace"])
            s_text += f"| {page} | {img.group(1) if img else '-'} | переснять | {op['what'].replace('|', '/')} | {uid} | {lang} |\n"
    save_text(trace, t_text)
    save_text(shots, s_text)
    return verified["ops"]


def verify_rn(answer: Dict[str, Any], expected: List[str]) -> Dict[str, Any]:
    """Release notes: по одной формулировке RU и EN на каждую принятую задачу, без лишних и пропущенных."""
    problems: List[str] = []
    lines: Dict[str, Dict[str, str]] = {}
    for n, x in enumerate(answer.get("lines") or [], 1):
        if not isinstance(x, dict) or not x.get("task"):
            problems.append(f"строка {n}: нужны поля task, ru, en")
            continue
        uid = str(x["task"])
        if uid in lines:
            problems.append(f"{uid}: строка дана дважды")
        lines[uid] = {"ru": str(x.get("ru") or "").strip(), "en": str(x.get("en") or "").strip()}
        for lang in ("ru", "en"):
            t = lines[uid][lang]
            if not t or "\n" in t:
                problems.append(f"{uid}: формулировка {lang} пустая или в несколько строк")
            if "`" in t or "<!--" in t or re.search(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b", t):
                problems.append(f"{uid}: в формулировке {lang} осталось имя из кода или служебная пометка")
        if not re.search(r"[А-Яа-я]", lines[uid]["ru"]) or re.search(r"[А-Яа-я]", lines[uid]["en"]):
            problems.append(f"{uid}: формулировки перепутаны по языкам или не переведены")
    missing = [u for u in expected if u not in lines]
    extra = [u for u in lines if u not in expected]
    if missing:
        problems.append("нет строк для принятых задач: " + ", ".join(missing))
    if extra:
        problems.append("даны строки для задач, которые в release notes не идут: " + ", ".join(extra))
    return {"problems": problems, "lines": lines}


def check_answer(path: Path, item: Dict[str, Any], ds=None) -> Dict[str, Any]:
    """Формальная проверка ответа: файл есть, это JSON, обязательные поля на месте, код правил верный."""
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
    return {"ok": True, "answer": a}


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


def touched_in_workspace(ds) -> List[str]:
    """Файлы документации, которые агент изменил в своей папке напрямую. Такие изменения не учитываются:
    правки принимаются только из ответов."""
    manifest_path = ds.agent_dir / "workspace_manifest.json"
    if not manifest_path.is_file():
        return []
    manifest = load_json(manifest_path)
    return [f"docs/{k}" for k, p in _doc_files(workspace(ds) / "docs").items() if manifest.get(k) != _digest(p)]


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


def run(ds, units: List[Dict[str, Any]], conf: Dict[str, Any], decisions: Dict[str, str], index: Dict[str, Any], *,
        dry_run: bool, cmd: Optional[str], redo: bool, timeout: int, recheck: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Готовит все задания, проверяет ответы и вносит принятые правки.

    Ответ, принятый в прошлый запуск, повторно не проверяется и не вносится: его правки уже в result/.
    """
    by_id = {u["id"]: u for u in units}
    command = agent_cmd(cmd)
    launch = bool(command) and not dry_run
    w = workspace(ds)
    final = dict(decisions)
    qpath = ds.agent_dir / "queue.json"
    prev = {i["id"]: i for i in (load_json(qpath)["items"] if qpath.is_file() else [])}
    touched = touched_in_workspace(ds)
    docs = Docs(ds)

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
    units2: Dict[str, Dict[str, Any]] = {}
    round2 = []
    for i in queue:
        if i["id"] in wanted and i["unit"]:
            first = w / "answers" / f"{i['id']}.json"
            u2 = dict(by_id[i["unit"]])
            u2["_round2"] = {"reason": wanted[i["id"]], "answer": json.loads(read_text(first) or "{}") if first.is_file() else {}}
            units2[i["id"] + "__round2"] = u2
            round2.append({"id": i["id"] + "__round2", "unit": i["unit"], "kind": i["kind"], "round": 2, "first": i["id"]})
    rn_items = [i for i in queue if i["kind"] == "release_notes"]
    items = [i for i in queue if i["kind"] != "release_notes"] + round2 + rn_items      # release notes всегда последними

    def unit_of(item):
        return units2.get(item["id"]) or (by_id.get(item["unit"]) if item["unit"] else None)

    def prompt_of(item) -> str:
        table = dict(by_id)
        if item["id"] in units2:
            table[item["unit"]] = units2[item["id"]]
        return render_task(ds, item, table, conf, final)

    prompts = {i["id"]: prompt_of(i) for i in items}
    if launch:
        refresh_workspace(ds, items, prompts, by_id)

    seconds: Dict[str, int] = {}
    errors: Dict[str, str] = {}
    for n, item in enumerate(items, 1):
        path = w / "answers" / f"{item['id']}.json"
        old = prev.get(item["id"], {})
        kept = (not redo and path.is_file() and old.get("status") in STATUSES and old.get("answer_sha") == _digest(path))
        if launch and not kept:
            if redo and path.exists():
                path.unlink()
            if not check_answer(path, item, ds)["ok"]:
                if path.exists():
                    path.unlink()
                log(f"агент: {item['id']} ({n}/{len(items)})")
                t0 = time.time()
                err = run_cmd(ds, item, prompt_of(item), command, timeout)
                seconds[item["id"]] = int(time.time() - t0)
                log(f"агент: {item['id']} - {seconds[item['id']]} с")
                if err:
                    errors[item["id"]] = err
        r = check_answer(path, item, ds)
        a = r.get("answer") or {}
        item.update(problems=r.get("problems", []), applied=old.get("applied", []) if kept else [], decision=None)
        if kept:
            item.update(status=old["status"], note=old.get("note", ""))
            if item["kind"] == "decide" and old.get("decision"):
                item["decision"] = old["decision"]
        elif r["ok"] and a["status"] == "needs_human":
            item.update(status="needs_human", note=str(a.get("summary", ""))[:300])
        elif r["ok"] and item["kind"] == "release_notes":
            left = [i["id"] for i in items if i["kind"] == "decide" and i.get("status") in ("prepared", "unverified", "failed")]
            expected = [u["id"] for u in units if final.get(u["id"]) in ("doc", "rn", "langs")]
            v = verify_rn(a, expected) if not left else {"problems": ["не завершены задания, от которых зависит состав "
                                                                     "строк: " + ", ".join(left)], "lines": {}}
            if v["problems"]:
                item.update(status="unverified", problems=v["problems"], note="не подтверждено: " + "; ".join(v["problems"][:4]))
            else:
                DRAFT.write_release_notes(ds, units, final, conf, index, final_lines=v["lines"])
                item.update(status=a["status"], note=str(a.get("summary", ""))[:300],
                            applied=[{"file": f"release notes: строк {len(v['lines'])}", "what": "файлы RU и EN собраны кодом"}])
        elif r["ok"]:
            v = verify_answer(ds, unit_of(item), item["kind"], a, docs, conf, item["id"])
            if v["problems"]:
                item.update(status="unverified", problems=v["problems"],
                            note="не подтверждено: " + "; ".join(v["problems"][:3])
                                 + (f"; и еще нарушений: {len(v['problems']) - 3}" if len(v["problems"]) > 3 else ""))
            else:
                ops = apply_ops(ds, docs, item["unit"], v)
                item.update(status=a["status"], note=str(a.get("summary", ""))[:300],
                            applied=[{"file": o["file"], "what": o["what"]} for o in ops])
                if item["kind"] == "decide":
                    item["decision"] = a["decision"]
        elif r.get("missing"):
            item.update(status="failed" if item["id"] in errors else "prepared", note=errors.get(item["id"], "ответа нет"))
        elif r.get("unverified"):
            item.update(status="unverified", note=r["error"])
        else:
            item.update(status="failed", note=errors.get(item["id"]) or r["error"])
        if item.get("round") == 2 and item["status"] == "recheck":
            item["status"] = "needs_human"                    # третьего круга нет
        if item["kind"] == "decide" and item["status"] in FINAL and item["decision"]:
            final[item["unit"]] = item["decision"]
        item["answer_sha"] = _digest(path) if path.is_file() else None
        item["recheck_reason"] = str(a.get("recheck_reason") or "") if item["status"] == "recheck" else ""
        item["log"] = [f"{st.get('source')}: {st.get('found')}" for st in (a.get("steps") or []) if isinstance(st, dict)][:30]
        item["questions"] = [str(x) for x in (a.get("questions") or [])]
        item["summary"] = str(a.get("summary", ""))[:300]

    # время: при ручном запуске оценивается по времени записи файлов ответов
    if not launch:
        mpath = ds.agent_dir / "workspace_manifest.json"
        start = mpath.stat().st_mtime if mpath.is_file() else None
        for t, name in sorted((p.stat().st_mtime, p.stem) for p in (w / "answers").glob("*.json")):
            if start is not None and t >= start:
                seconds[name] = int(t - start)
            start = t
    for item in items:
        old = prev.get(item["id"], {})
        item["seconds"] = seconds.get(item["id"], old.get("seconds") if old.get("answer_sha") == item["answer_sha"] else None)
    if (w / "progress.md").is_file():
        shutil.copyfile(w / "progress.md", ds.agent_dir / "progress.md")
    refresh_workspace(ds, items, {i["id"]: prompt_of(i) for i in items}, by_id)
    save_json(qpath, {"command": command, "launched": launch, "items": items, "touched": touched})
    save_json(ds.agent_dir / "final_decisions.json", final)
    return {"items": items, "final": final, "launched": launch, "command": command, "touched": touched}


def summary_md(ds, res: Dict[str, Any], decisions: Dict[str, str]) -> str:
    w = ds.rel(workspace(ds))
    counts: Dict[str, int] = {}
    for i in res["items"]:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    names = {"done": "принято", "recheck": "принято, нужен второй круг", "needs_human": "нужен человек",
             "unverified": "не подтверждено", "failed": "ошибка", "prepared": "ждет агента"}
    L = [f"# Журнал работы агента: набор данных {ds.name}", "",
         "Состояние заданий: " + (", ".join(f"{names[k]} - {v}" for k, v in sorted(counts.items())) or "заданий нет") + ".", ""]
    if not res["launched"] and counts.get("prepared"):
        L += [f"Агент запускается вручную. Откройте в редакторе только папку `{w}` и дайте агенту задания из "
              "`tasks/` по порядку из файла `AGENTS.md`. После этого запустите команду `agent` еще раз: она проверит "
              "ответы, внесет принятые правки и обновит этот журнал.", ""]
    total = sum(i.get("seconds") or 0 for i in res["items"])
    if total:
        L += [f"Время агента: {total // 60} мин {total % 60} с" + ("" if res["launched"] else " (оценка по времени записи ответов)")
              + ". Ход работы по шагам: `agent/progress.md`.", ""]
    again = [i for i in res["items"] if i["status"] == "recheck"]
    if again:
        L += ["**Агент просит второй круг** по заданиям: " + ", ".join(i["id"] for i in again)
              + ". Проверьте их ниже и запустите команду `agent` еще раз: для них будут подготовлены задания второго круга.", ""]
    redo = [i for i in res["items"] if i["status"] == "unverified"]
    if redo:
        L += ["**Нужно переделать** (" + str(len(redo)) + "): " + ", ".join(i["id"] for i in redo)
              + ". Причины ниже. Дайте агенту эти задания заново вместе с причинами.", ""]
    if res["touched"]:
        L += ["**Агент менял файлы документации напрямую.** Эти изменения не учтены: правки принимаются только из "
              "ответов. Файлы возвращены к принятому состоянию: " + ", ".join(f"`{f}`" for f in res["touched"][:12]) + ".", ""]
    L += ["| Задание | Состояние | Решение | Правок внесено | Время | Итог |", "| --- | --- | --- | --- | --- | --- |"]
    for i in res["items"]:
        sec = i.get("seconds")
        t = f"{sec // 60}:{sec % 60:02d}" if sec is not None else "-"
        L.append(f"| {i['id']} | {names[i['status']]} | {i.get('decision') or '-'} | {len(i.get('applied') or [])} | {t} | "
                 f"{(i.get('note') or '-').replace('|', '/')} |")
    detail = [i for i in res["items"] if i["status"] != "prepared"]
    if detail:
        L += ["", "## Подробности по заданиям", ""]
    for i in detail:
        L += [f"### {i['id']}: {names[i['status']]}", ""]
        if i.get("summary"):
            L += [f"Итог агента: {i['summary']}", ""]
        if i.get("recheck_reason"):
            L += [f"Сомнение агента: {i['recheck_reason']}", ""]
        if i["problems"]:
            L += ["Что не подтверждено:", ""] + [f"- {p}" for p in i["problems"]] + [""]
        if i.get("applied"):
            L += ["Внесено кодом:", ""] + [f"- `{a['file']}`: {a['what']}" for a in i["applied"]] + [""]
        if i["questions"]:
            L += ["Вопросы агента:", ""] + [f"- {q}" for q in i["questions"]] + [""]
        if i["log"]:
            L += ["Шаги агента (источник: что нашел):", ""] + [f"- {x}" for x in i["log"]] + [""]
    changed = {k: v for k, v in res["final"].items() if decisions.get(k) != v}
    if changed:
        L += ["", "## Решения агента", ""] + [f"- {k}: {V.DECISION_TEXT[v]}" for k, v in changed.items()]
    left = [k for k, v in res["final"].items() if v == "agent"]
    if left:
        L += ["", "Остались без решения: " + ", ".join(left) + "."]
    return "\n".join(L) + "\n"
