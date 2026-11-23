"""Задания агента: очередь, источники задачи, текст задания.

Агент получает отдельное задание на каждую находку и ровно столько данных, сколько нужно для нее:
записи списка изменений, тексты интерфейса, сводку измененных строк кода и страницы документации.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import docs as DOCS
from .. import draft as DRAFT
from .. import product as P
from .. import validation as V
from ..common import ROOT, load_json, read_text, save_text

PROMPTS = ROOT / "agent" / "prompts"
STALE_ID = "STALE"              # условная "задача" для проверки устаревших названий по всему релизу
CHECKS_ID = "CHECKS"             # условная "задача" для исправления находок сквозных проверок
KIND_ORDER = ["decide", "write_doc", "stale", "checks", "release_notes"]
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


OWNERS_PER_TEXT = 2             # задач-владельцев на одно устаревшее название
OWNER_FILES = 8                  # файлов кода одной задачи-владельца, которые добавляются к заданию об устаревших названиях


def stale_owners(x: Dict[str, Any], units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Задачи, в чьих изменениях интерфейса исчез или изменился этот текст: их записи и файлы объясняют, что стало вместо него."""
    out = []
    for u in units:
        i = u["i18n"]
        gone = [y for y in i["removed"] if y["locale"] == x["locale"] and y["value"] == x["text"]]
        gone += [y for y in i["changed"] if y["locale"] == x["locale"] and y["old"] == x["text"]]
        if gone:
            files = [f for f in u["files"] if f.get("relevant") and f.get("patch") and f["category"] in ("source", "i18n")]
            out.append({"id": u["id"], "size": len(i["removed"]) + len(i["changed"]),
                        "texts": (DRAFT.significant_texts(u) or [e["text"] for e in u["entries"]])[:2], "files": files[:OWNER_FILES]})
    # задача с широкой переработкой переводов (сотни текстов) убирает почти все названия: выше те, кто убрал меньше, то есть точнее
    return sorted(out, key=lambda o: (o["size"], o["id"]))[:OWNERS_PER_TEXT]


def stale_unit(ds, stale: List[Dict[str, Any]], units: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """Условная задача из списка устаревших названий: у нее те же поля, что у обычной."""
    if not stale:
        return None
    stale = [dict(x, owners=stale_owners(x, units or [])) for x in stale]
    files: Dict[str, Dict[str, Any]] = {}
    for x in stale:
        for f in x["files"] + [f for o in x["owners"] for f in o["files"]]:
            files.setdefault(f["path"], {"path": f["path"], "relevant": True, "patch": f.get("patch"),
                                         "category": f["category"], "status": f["status"], "reachable_from_app": True})
    return {"id": STALE_ID, "task": None, "_stale": stale, "entries": [], "files": list(files.values()),
            "i18n": {"added": [], "removed": [{"locale": x["locale"], "value": x["text"]} for x in stale if not x["replacement"]],
                     "changed": [{"locale": x["locale"], "old": x["text"], "new": x["replacement"]} for x in stale if x["replacement"]]},
            "doc_candidates": [{"doc": m["file"], "why": []} for x in stale for m in x["mentions"]], "jira": None}


def checks_unit(findings: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Условная задача из находок сквозных проверок: страницы - те, где находки."""
    if not findings:
        return None
    pages = sorted({x["file"] for x in findings})
    code = sorted({c for x in findings for c in x.get("code", [])})
    return {"id": CHECKS_ID, "task": None, "_checks": findings, "_code": code, "entries": [], "files": [],
            "i18n": {"added": [], "removed": [], "changed": []}, "doc_candidates": [{"doc": p, "why": []} for p in pages], "jira": None}


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


APP_LOCALE_FILES = ("apps/point/package.json", "apps/point/.env", "apps/point/.env.production")


def app_locales(root: Path) -> List[str]:
    """Языки, включенные в настройках приложения: vlabs.locales в package.json, иначе VITE_LOCALES в .env."""
    pkg = root / APP_LOCALE_FILES[0]
    if pkg.is_file():
        try:
            loc = (json.loads(read_text(pkg) or "{}").get("vlabs") or {}).get("locales")
        except ValueError:
            loc = None
        if loc:
            return [str(x) for x in loc]
    for name in APP_LOCALE_FILES[1:]:
        m = re.search(r"^\s*VITE_LOCALES\s*=\s*(\S+)", read_text(root / name) or "", flags=re.M)
        if m:
            return [x.strip() for x in m.group(1).strip("\"'").split(",") if x.strip()]
    return []


def app_locales_text(ds) -> str:
    """Факт для задачи о языках: какие языки включены в прошлой и новой версии (код сравнил настройки сам)."""
    old, new = app_locales(ds.slot(P.SLOT_CODE_OLD)), app_locales(ds.slot(P.SLOT_CODE_NEW))
    if not old and not new:
        return ""
    added, removed = [x for x in new if x not in old], [x for x in old if x not in new]
    return (f"языки, включенные в настройках приложения: прошлая версия - {', '.join(old) or 'не найдено'}; новая - {', '.join(new) or 'не найдено'}"
            + (f"; добавлены: {', '.join(added)}" if added else "") + (f"; убраны: {', '.join(removed)}" if removed else "")
            + ("; набор не изменился" if not added and not removed else ""))


def _mentions_languages(u: Dict[str, Any]) -> bool:
    return any("локал" in e["text"].lower() or "язык" in e["text"].lower() for e in u["entries"])


def unit_sources(ds, u: Dict[str, Any]) -> Dict[str, Path]:
    """Источники задачи: путь в папке агента -> настоящий файл. Первой идет сводка, затем патчи."""
    if u.get("_checks"):                             # находки и страницы напечатаны в задании; код - только для «мертвых» названий
        return {f"sources/{CHECKS_ID}/code/{c}": ds.slot(P.SLOT_CODE_NEW) / c for c in u.get("_code", [])}
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
    if u.get("_checks"):
        return ["", "## Страницы документации", "", "Правки можно предлагать только для этих страниц.", ""] \
            + [f"- `{p}`" for p in unit_pages(ds, u)] \
            + ["", "## Разрешенные источники", "",
               "Подтверждением служит дословная цитата со страницы из списка выше: строка с ошибкой и, если нужно, соседние "
               "страницы или другой язык. В подтверждениях указывай путь страницы точно так, как он записан выше."] \
            + ([f"- `sources/{CHECKS_ID}/code/` - код новой версии для находок «названия не было на экране и до релиза» "
                f"({len(u['_code'])} файлов). Ищи по ключу перевода или по слову, не читай все подряд. Папки: "
                + ", ".join(sorted({f"`sources/{CHECKS_ID}/code/{Path(c).parent}/`" for c in u["_code"]}))] if u.get("_code") else [])
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
    if _mentions_languages(u):
        fact = app_locales_text(ds)
        if fact:
            L += ["", f"Настройки языков (код сравнил обе версии): {fact}."]
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
        for o in x.get("owners", []):
            L.append(f"- текст исчез в задаче {o['id']}: " + ("; ".join(o["texts"]) or "записи нет") + ". Ее изменения кода есть в сводке и в патчах")
        L.append("- упоминания в документации:")
        L += [f"    - `docs/MD_{x['lang']}/docs/{m['file']}`:{m['line']}: {m['text'][:160]}" for m in x["mentions"]]
        L.append("")
    i = u["i18n"]
    L += ["Тексты интерфейса:", ""]
    L += [f"- удален [{x['locale']}]: «{x['value']}»" for x in i["removed"]]
    L += [f"- изменен [{x['locale']}]: «{x['old']}» -> «{x['new']}»" for x in i["changed"]]
    return "\n".join(L + _sources_block(ds, u))


VAR_LINE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _variables_hint(ds, limit: int = 20) -> List[str]:
    """Переменные документации и их значения: по ним видно, какой переменной можно заменить название слово в слово."""
    values = DOCS.ci_variables(ds.slot(P.SLOT_DOCS_OLD))
    if not values:
        return ["значения переменных неизвестны: заменяй, только если уверен, что переменная дает то же самое название"]
    return [f"- `{{{{ {k} }}}}` = {v}" for k, v in list(values.items())[:limit]
            if k.startswith(("PRODUCT_", "COMPANY_")) or "NAME" in k]


def _similar_pages(ds, target: str, limit: int = 5) -> List[str]:
    """Страницы, чье имя похоже на имя из битой ссылки: кандидаты на правильный адрес."""
    root = ds.result_dir / "MD_rus" / "docs"
    paths = sorted(str(p.relative_to(root)) for p in root.rglob("*.md")) if root.is_dir() else []
    close = difflib.get_close_matches(Path(target.partition("#")[0]).name, sorted({Path(n).name for n in paths}), n=limit, cutoff=0.5)
    return [n for n in paths if Path(n).name in close][:limit]


def _checks_context(ds, u: Dict[str, Any]) -> str:
    fx = u["_checks"]
    L = ["## Находки проверок документации", "",
         "Код проверил рабочую копию документации после всех правок. Ниже то, что нужно исправить правкой текста. «Новое» - "
         "появилось в этом релизе; остальное лежит на страницах, которые правились в этом релизе.", ""]
    names = {"links": "битая ссылка", "brands": "название продукта текстом", "images_missing": "нет файла картинки",
             "parity": "RU и EN расходятся", "rename": "название заменено в другой правке",
             "ui_name": "названия нет в интерфейсе",
             "dead_name": "названия не было на экране и до релиза"}
    for x in fx:
        where = f"`docs/MD_{x['lang'].split('/')[0]}/docs/{x['file']}`" + (f":{x['line']}" if x["line"] else "")
        L.append(f"- [{names[x['check']]}] {where}: `{x['target']}` - {x['why']}" + (" (новое)" if x["new"] else ""))
    if any(x["check"] == "links" for x in fx):
        L += ["", "Страницы с похожими именами для битых ссылок (проверь, что это та же страница, и бери путь только из этого списка или из текста самой страницы):", ""]
        for x in fx:
            if x["check"] == "links" and x["why"] != "подключаемого файла нет":
                sim = _similar_pages(ds, x["target"])
                L.append(f"- `{x['target']}`: " + (", ".join(f"`{n}`" for n in sim) if sim else "похожих страниц нет"))
    if any(x["check"] == "brands" for x in fx):
        L += ["", "Переменные документации и их значения. Название продукта заменяется переменной, только если ее значение совпадает с заменяемым текстом слово в слово; иначе не правь:", ""]
        L += _variables_hint(ds)
    if any(x["check"] == "dead_name" for x in fx):
        L += ["", "Для «названия не было на экране и до релиза»: текст есть в переводах прошлой версии, но код его не выводил. Найди в "
                  "приложенном коде новой версии, какой текст выводится на этом месте: в коде стоит ключ перевода, а не текст, поэтому "
                  "ищи сначала в файле переводов (первый в списке), затем ключ в коде. Если у находки указаны похожие тексты интерфейса и "
                  "один из них - тот же элемент (например, те же слова в другом порядке), замени на него. Подтверди цитатами. "
                  "Если не видно, не правь."]
    if any(x["check"] == "ui_name" for x in fx):
        L += ["", "Для «названия нет в интерфейсе»: название в кавычках не найдено в текстах интерфейса новой версии (документация могла "
                  "устареть еще до релиза). Если это название элемента интерфейса и один из похожих - тот же элемент с текущим названием, "
                  "замени на него. Если это не элемент интерфейса (значение, термин, название документа) или уверенной замены нет, не правь."]
    if any(x["check"] == "rename" for x in fx):
        L += ["", "Для «название заменено в другой правке»: другая принятая правка этого релиза заменила название элемента интерфейса. "
                  "Замени его и здесь, но только если упоминание относится к тому же элементу (тот же пункт меню, та же кнопка); "
                  "если это другой элемент с тем же названием, не трогай."]
    if any(x["check"] == "parity" for x in fx):
        L += ["", "Для расхождений RU и EN: русская страница главная, подгоняется английская. Чинишь только то, что видно по тексту "
                  "страниц (число разделов, картинок, примечаний, подключаемых файлов, версии). Нет факта - `unknown`."]
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
        hint = "fix" if DRAFT.is_fix_unit(u, decisions) else "change"
        L.append(f"- раздел по записям разработчиков (подсказка, решаешь по смыслу): {hint}")
        langs = app_locales_text(ds) if d == "langs" else ""
        if langs:
            L.append(f"- {langs}")
        title = DRAFT.jira_title(u)
        if title:
            L.append(f"- название задачи (внутреннее, не для пользователя): {title}")
        sig = [e for e in u["entries"] if e["_flags"].get("significant")] or u["entries"]
        L += [f"- запись разработчиков [{e['section']}]: {e['text'].strip()}" for e in sig[:10]]
        L.append("")
    return "\n".join(L)


def render_task(ds, item: Dict[str, Any], units_by_id: Dict[str, Any], conf: Dict[str, Any], decisions: Dict[str, str]) -> str:
    head = (read_text(PROMPTS / "task.md") or "").replace("{{ITEM}}", item["id"]).replace("{{KIND}}", item["kind"]) \
        .replace("{{ANSWER_FILE}}", f"answers/{item['id']}.json")
    if item["kind"] == "release_notes":
        body = _release_notes_context(ds, units_by_id, decisions)
    elif item["kind"] == "stale":
        body = _stale_context(ds, units_by_id[STALE_ID])
    elif item["kind"] == "checks":
        body = _checks_context(ds, units_by_id[CHECKS_ID])
    else:
        body = _unit_context(ds, units_by_id[item["unit"]], conf)
    retry = ""
    if item.get("retry_of"):
        retry = ("\n## Повторная попытка\n\nЧасть первого ответа не принята; принятое уже внесено, повторять его "
                 "не нужно. Причины:\n\n" + "\n".join(f"- {p}" for p in item["problems_before"][:12])
                 + "\n\nВ новом ответе дай только отклоненные правки или строки. Это последняя попытка.\n")
    return head.strip() + "\n" + retry + "\n" + body.strip() + "\n"
