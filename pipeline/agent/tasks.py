"""Задания агента: очередь, источники задачи, текст задания.

Агент получает отдельное задание на каждую находку и ровно столько данных, сколько нужно для нее:
записи списка изменений, тексты интерфейса, сводку измененных строк кода и страницы документации.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import draft as DRAFT
from .. import product as P
from .. import validation as V
from ..common import ROOT, load_json, read_text, save_text

PROMPTS = ROOT / "agent" / "prompts"
STALE_ID = "STALE"              # условная "задача" для проверки устаревших названий по всему релизу
KIND_ORDER = ["decide", "write_doc", "stale", "release_notes"]
DIGEST_LINES_PER_FILE = 60
DIGEST_LINES_TOTAL = 500
DIGEST_ORDER = {"source": 0, "i18n": 1, "style": 3}
DIGEST_SKIP = ("test", "build", "ci", "mocks", "changelog", "docs_internal", "types", "assets")
LANG_SETTINGS = ("apps/point/package.json", "apps/point/src/app/translations/i18n.ts")


def agent_cmd(cmd: Optional[str]) -> List[str]:
    """Команда запуска агента: параметр --cmd или переменная DOC_AGENT_CMD. Без нее задания только готовятся."""
    cmd = cmd or os.environ.get("DOC_AGENT_CMD")
    return shlex.split(cmd) if cmd else []


def workspace(ds) -> Path:
    return ds.run_dir / P.OUT_WORKSPACE


def unit_dir(uid: str) -> str:
    return uid.replace(":", "_").replace("#", "_")


# ---------------------------------------------------------------- очередь

def kinds_for(decision: str) -> List[str]:
    """decide - решение не подтверждено; write_doc - править документацию. Остальные решения заданий не требуют."""
    return {"agent": ["decide"], "doc": ["write_doc"], "langs": ["write_doc"]}.get(decision, [])


def build_queue(units: List[Dict[str, Any]], decisions: Dict[str, str], stale: bool) -> List[Dict[str, Any]]:
    items = [{"id": f"{unit_dir(u['id'])}__{k}", "unit": u["id"], "kind": k}
             for u in units for k in kinds_for(decisions.get(u["id"], "skip"))]
    if stale:
        items.append({"id": "stale_names", "unit": STALE_ID, "kind": "stale"})
    if any(decisions.get(u["id"]) in ("doc", "rn", "langs", "agent") for u in units):
        items.append({"id": "release_notes", "unit": None, "kind": "release_notes"})
    return sorted(items, key=lambda i: (KIND_ORDER.index(i["kind"]), i["id"]))


def stale_unit(ds, stale: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Условная задача из списка устаревших названий: у нее те же поля, что у обычной."""
    if not stale:
        return None
    files: Dict[str, Dict[str, Any]] = {}
    for x in stale:
        for f in x["files"]:
            files.setdefault(f["path"], {"path": f["path"], "relevant": True, "patch": f.get("patch"),
                                         "category": f["category"], "status": f["status"], "reachable_from_app": True})
    return {"id": STALE_ID, "task": None, "_stale": stale, "entries": [], "files": list(files.values()),
            "i18n": {"added": [], "removed": [{"locale": x["locale"], "value": x["text"]} for x in stale if not x["replacement"]],
                     "changed": [{"locale": x["locale"], "old": x["text"], "new": x["replacement"]} for x in stale if x["replacement"]]},
            "doc_candidates": [{"doc": m["file"], "why": []} for x in stale for m in x["mentions"]], "jira": None}


# ---------------------------------------------------------------- источники задачи

LINK_RANK = (("тег →", 0, "по тегу записи"), ("имя", 1, "по имени в записи"), ("путь по тегу", 2, "по части пути тега"))


def link_kind(f: Dict[str, Any]) -> tuple:
    """Как файл привязан к задаче: (надежность, подпись). Чем меньше число, тем надежнее связь."""
    best = (3, "")
    for text in f.get("linked_by", []):
        for key, rank, label in LINK_RANK:
            if key in text.split(": ", 1)[-1][:14] and rank < best[0]:
                best = (rank, label)
    return best if best[1] else (0, "")


def build_digest(ds, u: Dict[str, Any]) -> str:
    """Сводка изменений кода по задаче: только добавленные и удаленные строки из патчей, дословно."""
    data = ds.data_dir / "code"
    rel = [f for f in u["files"] if f.get("relevant") and f.get("patch") and f["category"] not in DIGEST_SKIP]
    # сначала файлы с самой надежной связью с задачей, затем код приложения, тексты интерфейса, стили
    rel.sort(key=lambda f: (link_kind(f)[0], DIGEST_ORDER.get(f["category"], 2), f["path"]))
    stale = bool(u.get("_stale"))
    L = ["# Изменения файлов, связанных с устаревшими названиями" if stale else f"# Изменения кода по задаче {u['id']}", "",
         "Только измененные строки: `+` добавлено, `-` удалено."
         + ("" if stale else " У каждого файла указано, использует ли его приложение POINT 2 (по связям между файлами кода)."), ""]
    total, shown = 0, 0
    other = ds.rules["product"]["other_product_file_markers"]
    for f in rel:
        if total >= DIGEST_LINES_TOTAL:
            break
        lines = [x for x in (read_text(data / f["patch"]) or "").splitlines()
                 if x.startswith("@@") or ((x.startswith("+") or x.startswith("-")) and not x.startswith(("+++", "---")))]
        cut = lines[:min(DIGEST_LINES_PER_FILE, DIGEST_LINES_TOTAL - total)]
        if stale:
            note = ""
        elif any(m in f["path"] for m in other):
            note = ", файл другого продукта"
        else:
            note = ", используется приложением POINT 2" if f["reachable_from_app"] else ", приложение POINT 2 этот файл не использует"
        link = link_kind(f)[1]
        note += f", связь с задачей: {link}" if link and not stale else ""
        L += [f"## {f['path']} [{f['category']}, {f['status']}{note}]", "", "```diff", *cut, "```"]
        if len(cut) < len(lines):
            L.append(f"Показано строк: {len(cut)} из {len(lines)}. Полностью: `patches/{Path(f['patch']).name}`.")
        L.append("")
        total += len(cut)
        shown += 1
    if shown < len(rel):
        L += [f"Не показано файлов: {len(rel) - shown} из {len(rel)}. Их патчи лежат в `patches/`.", ""]
    if not rel:
        L += ["Измененных файлов кода нет.", ""]
    return "\n".join(L)


def _mentions_languages(u: Dict[str, Any]) -> bool:
    return any("локал" in e["text"].lower() or "язык" in e["text"].lower() for e in u["entries"])


def unit_sources(ds, u: Dict[str, Any]) -> Dict[str, Path]:
    """Источники задачи: путь в папке агента -> настоящий файл. Первой идет сводка, затем патчи."""
    base = f"sources/{unit_dir(u['id'])}"
    digest = ds.data_dir / "agent_digest" / f"{unit_dir(u['id'])}.md"
    save_text(digest, build_digest(ds, u))           # всегда заново: привязка файлов к задаче могла измениться
    out: Dict[str, Path] = {f"{base}/changes.md": digest}
    for f in u["files"]:
        if f.get("relevant") and f.get("patch"):
            out[f"{base}/patches/{Path(f['patch']).name}"] = ds.data_dir / "code" / f["patch"]
    if _mentions_languages(u):                       # набор языков задается в настройках приложения
        for p in LANG_SETTINGS:
            if (ds.slot(P.SLOT_CODE_NEW) / p).is_file():
                out[f"{base}/code/{p}"] = ds.slot(P.SLOT_CODE_NEW) / p
    return out


def unit_pages(ds, u: Dict[str, Any]) -> List[str]:
    """Страницы документации задачи в папке агента (на обоих языках): только существующие."""
    pages = sorted({c["doc"] for c in u["doc_candidates"]})
    return [f"docs/MD_{lang}/docs/{p}" for lang in ds.langs for p in pages
            if (ds.result_dir / f"MD_{lang}" / "docs" / p).is_file()]


# ---------------------------------------------------------------- текст задания

def index_of(ds) -> Dict[str, Any]:
    if not hasattr(ds, "_index"):
        p = ds.data_dir / "06_docs_index.json"
        ds._index = load_json(p) if p.is_file() else {}
    return ds._index


def _outline(ds, u: Dict[str, Any]) -> List[str]:
    """Заголовки страниц задачи с номерами строк: читать страницу целиком не нужно."""
    pages = {c["doc"] for c in u["doc_candidates"]}
    out = []
    for f in index_of(ds).get("rus", {}).get("files", []):
        if f["path"] in pages:
            heads = [f"строка {h['line']}: {h['text']}" for h in f["headings"] if h["level"] <= 3][:25]
            out.append(f"- `docs/MD_rus/docs/{f['path']}` ({f['lines']} строк): " + "; ".join(heads))
    return out


def _sources_block(ds, u: Dict[str, Any]) -> List[str]:
    srcs = list(unit_sources(ds, u))
    L = ["", "## Страницы документации", "", "Правки можно предлагать только для этих страниц.", ""]
    L += [f"- `{p}`" for p in unit_pages(ds, u)] or ["Страниц нет."]
    L += ["", "## Разрешенные источники", "",
          "Открывай только их. В подтверждениях указывай источник точно так, как он записан здесь.", ""]
    if not u.get("_stale"):
        L += ["- `changelog` - записи списка изменений задачи (выше)", "- `i18n` - ее тексты интерфейса (выше)"]
        if u.get("jira"):
            L.append("- `jira` - название и описание задачи в Jira (выше)")
    else:
        L.append("- `i18n` - список исчезнувших и измененных текстов (выше)")
    L.append(f"- `{srcs[0]}` - сводка изменений кода")
    patches = [x for x in srcs[1:] if "/patches/" in x]
    if patches:
        L.append(f"- `sources/{unit_dir(u['id'])}/patches/` - полные патчи ({len(patches)}); нужны, только если строка в сводке обрезана")
    L += [f"- `{x}`" for x in srcs[1:] if "/patches/" not in x]
    return L


def _unit_context(ds, u: Dict[str, Any], conf: Dict[str, Any]) -> str:
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
    if u.get("jira"):
        L += ["", f"Задача в Jira: {u['jira'].get('summary') or ''}", "", (u["jira"].get("description") or "")[:1500]]
    src_files = [f for f in u["files"] if f.get("relevant") and f["category"] == "source"]
    used = sum(1 for f in src_files if f["reachable_from_app"])
    L += ["", f"Файлов кода у значимых записей: {len(src_files)}, из них приложение POINT 2 использует: {used}. Это установлено "
              "по связям между файлами: если хотя бы один файл используется, изменение доходит до POINT 2."]
    outline = _outline(ds, u)
    return "\n".join(L + _sources_block(ds, u) + (["", "Заголовки страниц:", ""] + outline if outline else []))


def _stale_context(ds, u: Dict[str, Any]) -> str:
    L = ["## Устаревшие названия", "",
         "Эти тексты были в интерфейсе предыдущей версии, а в новой их нет. Документация их еще упоминает. К задачам релиза "
         "список не привязан. Для каждого названия указано, в каких файлах кода оно использовалось: в патчах этих файлов "
         "видно, что стоит на его месте теперь.", ""]
    for x in u["_stale"]:
        repl = f"у того же элемента теперь: «{x['replacement']}»" if x["replacement"] else "замена в данных не найдена: ищи в патчах файлов ниже"
        L += [f"### [{x['lang']}] «{x['text']}»", "", f"- ключ перевода: `{x['where'][0]}`", f"- {repl}"]
        if x["used_in"]:
            L.append("- использовалось в: " + ", ".join(f"`{p}`" for p in x["used_in"][:6]))
        else:
            L.append("- использование ключа в коде прежней версии не найдено: возможно, ключ не использовался; "
                     "правь, только если по патчам видно, какой элемент интерфейса заменил название")
        L.append("- упоминания в документации:")
        L += [f"    - `docs/MD_{x['lang']}/docs/{m['file']}`:{m['line']}: {m['text'][:160]}" for m in x["mentions"]]
        L.append("")
    i = u["i18n"]
    L += ["Тексты интерфейса:", ""]
    L += [f"- удален [{x['locale']}]: «{x['value']}»" for x in i["removed"]]
    L += [f"- изменен [{x['locale']}]: «{x['old']}» -> «{x['new']}»" for x in i["changed"]]
    return "\n".join(L + _sources_block(ds, u))


def _release_notes_context(ds, units: Dict[str, Any], decisions: Dict[str, str]) -> str:
    v = ds.versions
    prev = f"docs/MD_rus/docs/{P.RELEASE_NOTES_DIR}/{v['previous_release_notes_file']}"
    L = ["## Задачи для release notes", "",
         f"Образец стиля: `{prev}` и тот же файл в `docs/MD_eng/`.", "",
         "Разделы для поля `section` (слово в слово):", ""] + [f"- {x}" for x in DRAFT.section_choices(index_of(ds))] + [""]
    for uid, d in decisions.items():
        if d not in ("doc", "rn", "langs", "agent"):
            continue
        u = units[uid]
        state = {"doc": "изменение", "langs": "изменение: набор языков", "rn": "исправление ошибки",
                 "agent": "решение принимаешь ты в задании decide: давай строку, только если решил doc, rn или langs"}[d]
        L += [f"### {uid} ({state})", ""]
        title = DRAFT.jira_title(u)
        if title:
            L.append(f"- название задачи (внутреннее, не для пользователя): {title}")
        L += [f"- запись разработчиков: {t}" for t in (DRAFT.significant_texts(u) or [e["text"] for e in u["entries"]])[:10]]
        L.append("")
    return "\n".join(L)


def render_task(ds, item: Dict[str, Any], units_by_id: Dict[str, Any], conf: Dict[str, Any], decisions: Dict[str, str]) -> str:
    head = (read_text(PROMPTS / "task.md") or "").replace("{{ITEM}}", item["id"]).replace("{{KIND}}", item["kind"]) \
        .replace("{{ANSWER_FILE}}", f"answers/{item['id']}.json")
    if item["kind"] == "release_notes":
        body = _release_notes_context(ds, units_by_id, decisions)
    elif item["kind"] == "stale":
        body = _stale_context(ds, units_by_id[STALE_ID])
    else:
        body = _unit_context(ds, units_by_id[item["unit"]], conf)
    retry = ""
    if item.get("retry_of"):
        retry = ("\n## Повторная попытка\n\nЧасть первого ответа не принята; принятое уже внесено, повторять его "
                 "не нужно. Причины:\n\n" + "\n".join(f"- {p}" for p in item["problems_before"][:12])
                 + "\n\nВ новом ответе дай только отклоненные правки или строки. Это последняя попытка.\n")
    return head.strip() + "\n" + retry + "\n" + body.strip() + "\n"
