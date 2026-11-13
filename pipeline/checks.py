"""Сквозные проверки документации: не привязаны к задачам, агент не нужен.

1. Битые ссылки: внутренние ссылки на страницы и разделы, которых нет; подключаемые файлы, которых нет.
2. Названия продуктов текстом: «LUNA», «VisionLabs» вместо переменных документации.
3. Расхождения языков: страница есть не во всех языках; разное число разделов, строк таблиц, картинок;
   разные значения версий компонентов.
5. Скриншоты к проверке: изменился текст раздела - скриншоты этого раздела нужно проверить.
4. Картинки: ссылки на файлы, которых нет; картинки, на которые никто не ссылается; файлы из includes,
   которые никуда не подключены.

Проверяется результат (output/<набор>/result). Те же проверки выполняются для документации прошлого
релиза: по разнице видно, что появилось в этом релизе, а что было и раньше.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import docs as DOCS
from . import product as P
from .common import read_text

LINK_RE = re.compile(r"(!?)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
INCLUDE_RE = re.compile(r"""\{%\s*include\s+['"]([^'"]+)['"]""")
ANCHOR_RE = re.compile(r"\{#([\w\-]+)\}")
HEADING_RE = re.compile(r"^(#{1,6})\s+\S")
VERSION_RE = re.compile(r"\b([A-Z][A-Z0-9_]*_VER)\s*=\s*([\w.\-]+)")
BRAND_RE = re.compile(r"\b(LUNA|VisionLabs)\b")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg")
EXTERNAL = ("http://", "https://", "mailto:", "ftp://")


def _pages(root: Path) -> Dict[str, List[str]]:
    out = {}
    if root.is_dir():
        for p in sorted(root.rglob("*.md")):
            out[p.relative_to(root).as_posix()] = (read_text(p) or "").splitlines()
    return out


def _image_files(root: Path) -> Set[str]:
    if not root.is_dir():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES}


def _norm(path: str) -> str:
    parts: List[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def _includers(pages: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """Какие страницы подключают файл: картинка в подключаемом файле ищется относительно них."""
    out: Dict[str, List[str]] = {}
    for page, lines in pages.items():
        for line in lines:
            for inc in INCLUDE_RE.findall(line):
                out.setdefault(_norm(str(Path(page).parent / inc)), []).append(page)
    return out


def _image_targets(page: str, link: str, includers: Dict[str, List[str]]) -> List[str]:
    """Возможные места картинки. Сайт собирается так, что страница становится папкой: '../Imgs/x.png' со
    страницы user-manual/100_Search.md - это user-manual/Imgs/x.png."""
    hosts = [page] + includers.get(page, [])
    out = []
    for h in hosts:
        d = Path(h).parent
        out.append(_norm(str(d / Path(h).stem / link)))
        out.append(_norm(str(d / link)))
    return out


def check_tree(root: Path, image_root: Path, lang: str) -> Dict[str, List[Dict[str, Any]]]:
    """Проверки внутри одного языка."""
    pages = _pages(root)
    images = _image_files(image_root)
    includers = _includers(pages)
    anchors = {page: {a for line in lines for a in ANCHOR_RE.findall(line)} for page, lines in pages.items()}
    links: List[Dict[str, Any]] = []
    brands: List[Dict[str, Any]] = []
    img_missing: List[Dict[str, Any]] = []
    used: Set[str] = set()
    orphans = sorted(p for p in pages if p.split("/")[0] == "includes" and p not in includers)
    for page, lines in pages.items():
        in_code = False
        for n, line in enumerate(lines, 1):
            if line.lstrip().startswith("```"):
                in_code = not in_code
            for inc in INCLUDE_RE.findall(line):
                target = _norm(str(Path(page).parent / inc))
                if target not in pages:
                    links.append({"lang": lang, "file": page, "line": n, "target": inc, "why": "подключаемого файла нет"})
            for bang, target in LINK_RE.findall(line):
                if target.startswith(EXTERNAL) or "{{" in target:
                    continue
                path, _, anchor = target.partition("#")
                if bang or path.lower().endswith(IMAGE_SUFFIXES):
                    if page in orphans:
                        continue                      # файл никуда не подключен: его картинки искать негде
                    cands = _image_targets(page, path, includers)
                    hit = next((c for c in cands if c in images), None)
                    if hit:
                        used.add(hit)
                    else:
                        img_missing.append({"lang": lang, "file": page, "line": n, "target": path, "why": "файла картинки нет"})
                    continue
                dest = page
                if path:
                    if not path.endswith(".md"):
                        continue                      # ссылка на собранный сайт или файл не из исходников
                    dest = _norm(str(Path(page).parent / path))
                    if dest not in pages:
                        links.append({"lang": lang, "file": page, "line": n, "target": target, "why": "страницы нет"})
                        continue
                if anchor and re.fullmatch(r"[a-z0-9\-_]+", anchor) and anchors.get(dest) and anchor not in anchors[dest]:
                    links.append({"lang": lang, "file": page, "line": n, "target": target, "why": "раздела с таким якорем нет"})
            if not in_code and P.RELEASE_NOTES_DIR not in page.split("/"):
                clean = re.sub(r"\{\{[^}]*\}\}|\]\([^)]*\)|`[^`]*`", "", line)
                m = BRAND_RE.search(clean)
                if m:
                    brands.append({"lang": lang, "file": page, "line": n, "target": m.group(1), "why": line.strip()[:160]})
    unused = [{"lang": lang, "file": f, "line": 0, "target": f, "why": "на картинку нет ссылок"} for f in sorted(images - used)]
    unused += [{"lang": lang, "file": f, "line": 0, "target": f, "why": "файл из includes не подключен ни одной страницей"}
               for f in orphans]
    return {"links": links, "brands": brands, "images_missing": img_missing, "images_unused": unused}


def _shape(lines: List[str]) -> Dict[str, int]:
    heads = sum(1 for l in lines if HEADING_RE.match(l))
    imgs = sum(1 for l in lines for bang, _ in LINK_RE.findall(l) if bang)
    incs = sum(len(INCLUDE_RE.findall(l)) for l in lines)
    notes = sum(1 for l in lines if l.startswith("!!!"))
    # строки таблиц не сравниваются: одна и та же таблица переносится по строкам в языках по-разному
    return {"разделов": heads, "картинок": imgs, "подключаемых файлов": incs, "примечаний": notes}


def check_parity(roots: Dict[str, Path]) -> List[Dict[str, Any]]:
    """Расхождения между языками: состав страниц, их устройство и версии компонентов."""
    langs = list(roots)
    if len(langs) < 2:
        return []
    pages = {lang: _pages(roots[lang]) for lang in langs}
    out: List[Dict[str, Any]] = []
    a, b = langs[0], langs[1]
    for page in sorted(set(pages[a]) | set(pages[b])):
        if P.RELEASE_NOTES_DIR in page.split("/"):
            continue
        if page not in pages[a] or page not in pages[b]:
            miss = a if page not in pages[a] else b
            out.append({"lang": f"{a}/{b}", "file": page, "line": 0, "target": "страница", "why": f"нет в {miss}"})
            continue
        sa, sb = _shape(pages[a][page]), _shape(pages[b][page])
        diff = [f"{k}: {a} {sa[k]}, {b} {sb[k]}" for k in sa if sa[k] != sb[k]]
        if diff:
            out.append({"lang": f"{a}/{b}", "file": page, "line": 0, "target": "устройство страницы", "why": "; ".join(diff)})
        va = {m.group(1): m.group(2) for l in pages[a][page] for m in VERSION_RE.finditer(l)}
        vb = {m.group(1): m.group(2) for l in pages[b][page] for m in VERSION_RE.finditer(l)}
        for k in sorted(set(va) | set(vb)):
            if va.get(k) != vb.get(k):
                out.append({"lang": f"{a}/{b}", "file": page, "line": 0, "target": k,
                            "why": f"версия: {a} {va.get(k, 'нет')}, {b} {vb.get(k, 'нет')}"})
    return out


SCREENSHOT_RE = re.compile(r"^\s*!\[([^\]]*)\]\(([^)\s]+)\)\s*$")


def screenshots_to_review(ds, style_pages: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Скриншоты, которые нужно проверить: сквозная проверка, агент в ней не участвует (изменение 24).

    Правило: если в разделе страницы изменился текст, скриншоты этого раздела считаются затронутыми. Если в
    самом разделе скриншотов нет, берутся скриншоты раздела уровнем выше, и так до всей страницы. Страница не
    менялась - ее скриншоты остаются прежними. Скриншотом считается картинка, стоящая отдельной строкой:
    значки внутри текста не учитываются. Замена номера версии компонента изменением экрана не считается.

    style_pages - страницы задач, по которым решено "изменилось только оформление": текст на них не меняется,
    поэтому к проверке идут все скриншоты страницы.
    """
    import difflib
    base_repo = ds.slot(P.SLOT_DOCS_OLD)
    out: List[Dict[str, Any]] = []
    for lang in ds.langs:
        old_pages = _pages(DOCS.docs_root(base_repo, lang))
        new_pages = _pages(DOCS.workspace_root(ds.result_dir, lang))
        for page in sorted(new_pages):
            if P.RELEASE_NOTES_DIR in page.split("/"):
                continue
            old, new = old_pages.get(page, []), new_pages[page]
            style_task = (style_pages or {}).get(page)
            if old == new and not style_task:
                continue
            changed: Set[int] = set()
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
                if tag == "replace" and i2 - i1 == j2 - j1 and all(
                        VERSION_RE.sub("", a) == VERSION_RE.sub("", b) for a, b in zip(old[i1:i2], new[j1:j2])):
                    continue                          # изменился только номер версии компонента
                if tag in ("replace", "insert"):
                    changed.update(range(j1, j2))
                elif tag == "delete":
                    changed.add(min(j1, len(new) - 1) if new else 0)
            heads = [(i, len(m.group(1)), new[i].lstrip("#").strip()) for i, l in enumerate(new) for m in [HEADING_RE.match(l)] if m]
            shots = [(i, m.group(1), m.group(2)) for i, l in enumerate(new) for m in [SCREENSHOT_RE.match(l)] if m]

            def span(idx: int, level: int) -> tuple:
                """Границы раздела уровня не глубже level, в который попадает строка idx."""
                start, title = 0, "(начало страницы)"
                for i, lv, t in heads:
                    if i <= idx and lv <= level:
                        start, title, cur = i, t, lv
                end = len(new)
                for i, lv, _ in heads:
                    if i > idx and lv <= level:
                        end = i
                        break
                return start, end, title

            found: Dict[int, Dict[str, Any]] = {}
            for idx in sorted(changed):
                for level in (6, 5, 4, 3, 2, 1, 0):
                    start, end, title = (0, len(new), "(вся страница)") if level == 0 else span(idx, level)
                    inside = [x for x in shots if start <= x[0] < end]
                    if inside:
                        for line, alt, path in inside:
                            e = found.setdefault(line, {"lang": lang, "file": page, "line": line + 1, "image": path,
                                                        "caption": alt, "section": title, "changed_lines": 0})
                            e["changed_lines"] += 1
                        break
            if style_task:
                for line, alt, path in shots:
                    found.setdefault(line, {"lang": lang, "file": page, "line": line + 1, "image": path, "caption": alt,
                                            "section": f"вся страница: по задаче {style_task} изменилось оформление",
                                            "changed_lines": 0})
            out += [found[k] for k in sorted(found)]
    return out


def render_screenshots(ds, shots: List[Dict[str, Any]]) -> str:
    L = ["# Скриншоты к проверке", "",
         "Список собран кодом. Правило: в разделе страницы изменился текст - скриншоты этого раздела нужно "
         "проверить и при необходимости переснять. Скриншоты страниц и разделов, где текст не менялся, остаются "
         "прежними. Что изображено на картинке, код не знает: решение, переснимать или нет, принимает человек.", "",
         "| Язык | Страница | Раздел | Картинка | Подпись | Изменено строк в разделе |", "| --- | --- | --- | --- | --- | --- |"]
    for x in shots:
        L.append(f"| {x['lang']} | `{x['file']}`:{x['line']} | {x['section'].replace('|', '/')} | `{x['image']}` | "
                 f"{x['caption'].replace('|', '/') or '-'} | {x['changed_lines']} |")
    if not shots:
        L.append("| - | - | - | - | - | - |")
    return "\n".join(L) + "\n"


CHECKS = [
    ("links", "Битые ссылки", "Ссылка на страницу, раздел или подключаемый файл, которых нет"),
    ("brands", "Названия продуктов текстом", "«LUNA» или «VisionLabs» вне переменных документации"),
    ("parity", "Расхождения языков", "Страница отличается по устройству или версиям между языками"),
    ("images_missing", "Картинок нет", "Ссылка на файл картинки, которого нет"),
    ("images_unused", "Лишние файлы", "Картинка, на которую нет ссылок, или файл из includes, который никуда не подключен"),
]


def _key(x: Dict[str, Any]) -> tuple:
    return (x["lang"], x["file"], x["target"], x["why"])


FIXABLE = ("links", "brands", "images_missing", "parity")
MAX_FIXES = 40


def findings_to_fix(res: Dict[str, Any], touched: Set[str]) -> List[Dict[str, Any]]:
    """Находки, которые исправляются правкой текста: появились в этом релизе или лежат на страницах, которые правил пайплайн.

    Остальное было в документации и до релиза: массовую правку чужих страниц пайплайн не начинает. Отсутствие страницы
    в одном из языков и картинки без ссылок правкой текста не исправляются. Не более MAX_FIXES, сначала новые.
    """
    out = []
    for k in FIXABLE:
        for x in res.get(k, {}).get("items", []):
            if x["target"] == "страница":
                continue
            if x["new"] or x["file"] in touched:
                out.append(dict(x, check=k))
    out.sort(key=lambda x: (not x["new"], x["check"], x["file"], x["line"]))
    return out[:MAX_FIXES]


def run_checks(ds) -> Dict[str, Any]:
    """Проверяет результат и документацию прошлого релиза. У каждой находки пометка new: появилась в этом релизе."""
    base_repo = ds.slot(P.SLOT_DOCS_OLD)

    def collect(root_of) -> Dict[str, List[Dict[str, Any]]]:
        res: Dict[str, List[Dict[str, Any]]] = {k: [] for k, _, _ in CHECKS}
        roots = {}
        for lang in ds.langs:
            root = root_of(lang)
            roots[lang] = root
            # картинки в рабочую копию не копируются: файлы берутся из документации прошлого релиза
            one = check_tree(root, DOCS.docs_root(base_repo, lang), lang)
            for k, v in one.items():
                res[k] += v
        res["parity"] = check_parity(roots)
        return res

    base = collect(lambda lang: DOCS.docs_root(base_repo, lang))
    result = collect(lambda lang: DOCS.workspace_root(ds.result_dir, lang))
    out: Dict[str, Any] = {}
    for k, _, _ in CHECKS:
        old = {_key(x) for x in base[k]}
        items = [dict(x, new=_key(x) not in old) for x in result[k]]
        fixed = [x for x in base[k] if _key(x) not in {_key(y) for y in result[k]}]
        out[k] = {"items": items, "new": sum(1 for x in items if x["new"]), "fixed": len(fixed)}
    return out


def render_md(ds, res: Dict[str, Any]) -> str:
    L = [f"# Сквозные проверки документации: релиз {ds.release}", "",
         "Проверки не привязаны к задачам и выполняются кодом. Проверяется результат: рабочая копия документации "
         "со всеми внесенными правками. «Новое» - появилось в этом релизе; остальное было и в документации "
         "прошлого релиза.", "",
         "| Проверка | Найдено | Из них новое | Исправлено в этом релизе |", "| --- | --- | --- | --- |"]
    for k, title, _ in CHECKS:
        r = res[k]
        L.append(f"| {title} | {len(r['items'])} | {r['new']} | {r['fixed']} |")
    shots = res.get("screenshots", [])
    L += ["", f"Скриншотов к проверке: {len(shots)}. Список: `result/screenshots.md`."]
    for k, title, what in CHECKS:
        r = res[k]
        L += ["", f"## {title}", "", what + ".", ""]
        if not r["items"]:
            L.append("Не найдено.")
            continue
        if k in ("brands", "images_unused"):
            # много однотипных находок: сводка по страницам, полностью только новое
            per: Dict[str, int] = {}
            for x in r["items"]:
                key = f"[{x['lang']}] {x['file']}" if k == "brands" else f"[{x['lang']}] {str(Path(x['file']).parent)}"
                per[key] = per.get(key, 0) + 1
            L += ["| Где | Сколько |", "| --- | --- |"] + [f"| `{f}` | {n} |" for f, n in sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))]
            new = [x for x in r["items"] if x["new"]]
            if k == "images_unused":
                inc = [x for x in r["items"] if x["why"].startswith("файл из includes")]
                if inc:
                    L += ["", "Файлы из includes, которые никуда не подключены:", ""] + [f"- [{x['lang']}] `{x['file']}`" for x in inc]
            if new:
                L += ["", "Новое:", ""] + [f"- [{x['lang']}] `{x['file']}`" + (f":{x['line']}" if x["line"] else "") + f": {x['why']}" for x in new]
            continue
        L += ["| Язык | Страница | Что | Подробности | Новое |", "| --- | --- | --- | --- | --- |"]
        for x in r["items"]:
            where = f"`{x['file']}`" + (f":{x['line']}" if x["line"] else "")
            L.append(f"| {x['lang']} | {where} | `{x['target']}` | {x['why'].replace('|', '/')} | {'да' if x['new'] else ''} |")
    return "\n".join(L) + "\n"
