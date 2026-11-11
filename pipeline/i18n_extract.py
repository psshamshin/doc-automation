"""Строки интерфейса из TS-файлов переводов и их дифф между версиями (правила Г1, Д4, Д9).

Файлы переводов — TypeScript-объекты вида
    export const ruRu = { search: { title: 'Поиск' } }
Разбираем легковесным токенайзером (без node): ключевой путь + строковое значение + локаль.

Дифф строится двумя способами:
* по значениям — какие тексты появились/исчезли в локали (устойчиво к рефакторингу ключей);
* по ключам — где у того же ключа в том же файле поменялся текст (переименования).
"""

from __future__ import annotations

import fnmatch
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .common import read_text, walk_files

IDENT_RE = re.compile(r"[^\W\d][\w$]*|\$[\w$]*")  # Unicode: в старых версиях ключи бывали кириллицей


def tokenize(src: str) -> List[Tuple[str, str]]:
    toks: List[Tuple[str, str]] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"`":
            q, j, buf = c, i + 1, []
            while j < n and src[j] != q:
                if src[j] == "\\" and j + 1 < n:
                    nxt = src[j + 1]
                    buf.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
                    j += 2
                    continue
                buf.append(src[j])
                j += 1
            toks.append(("str", "".join(buf)))
            i = j + 1
        elif (c.isalpha() or c in "_$") and IDENT_RE.match(src, i):
            m = IDENT_RE.match(src, i)
            toks.append(("id", m.group(0)))
            i = m.end()
        elif c.isdigit():
            j = i
            while j < n and (src[j].isalnum() or src[j] == "."):
                j += 1
            toks.append(("num", src[i:j]))
            i = j
        elif src.startswith("...", i):
            toks.append(("p", "..."))
            i += 3
        else:
            toks.append(("p", c))
            i += 1
    return toks


def locale_of(name: str, locale_names: Dict[str, List[str]]) -> Optional[str]:
    for loc, names in locale_names.items():
        if name in names:
            return loc
    return None


DIR_LOCALES = {"ru": "ru", "en": "en", "pt": "pt", "es": "es", "ptBr": "pt", "esEs": "es", "ruRu": "ru", "enUs": "en"}


def locale_from_path(rel: str) -> Optional[str]:
    for seg in rel.split("/")[:-1]:
        if seg in DIR_LOCALES:
            return DIR_LOCALES[seg]
    return None


def extract_file(src: str, locale_names: Dict[str, List[str]], rel: str = "") -> List[Dict[str, str]]:
    """Плоский список {locale, key, value} для одного файла.

    Локаль берётся из имени экспорта (ruRu, en, ptBr…), иначе из каталога (translations/ru/…),
    иначе 'shared' (общие объекты вроде `const reused = {...}`).
    Обходятся все объектные литералы выражения: `merge({}, base, {...})` тоже.
    """
    toks = tokenize(src)
    out: List[Dict[str, str]] = []
    dir_loc = locale_from_path(rel)
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t == ("id", "const") and i + 1 < n and toks[i + 1][0] == "id":
            name = toks[i + 1][1]
            loc = locale_of(name, locale_names) or dir_loc or "shared"
            j = i + 2
            while j < n and toks[j] not in (("p", "="), ("p", ";")):
                j += 1
            depth = 0
            while j < n:
                tok = toks[j]
                if tok == ("p", "{"):
                    j = _walk_object(toks, j, loc, out)
                    continue
                if tok in (("p", "("), ("p", "[")):
                    depth += 1
                elif tok in (("p", ")"), ("p", "]")):
                    depth -= 1
                elif depth <= 0 and (tok == ("p", ";") or tok in (("id", "export"), ("id", "const"))):
                    break
                j += 1
            i = max(j, i + 1)
            continue
        i += 1
    return out


def _walk_object(toks, j: int, loc: str, out: List[Dict[str, str]]) -> int:
    """Обход объектного литерала от '{' до парной '}'. Возвращает индекс после неё."""
    stack: List[str] = []
    pending: Optional[str] = None
    n = len(toks)
    depth = 0
    while j < n:
        typ, val = toks[j]
        if (typ, val) == ("p", "{"):
            depth += 1
            stack.append(pending or "")
            pending = None
        elif (typ, val) == ("p", "}"):
            depth -= 1
            if stack:
                stack.pop()
            pending = None
            if depth == 0:
                return j + 1
        elif typ in ("id", "str", "num") and j + 1 < n and toks[j + 1] == ("p", ":"):
            pending = val
            j += 2
            if j < n and toks[j][0] == "str":
                # значение-строка: проверяем, что дальше разделитель, а не выражение
                nxt = toks[j + 1] if j + 1 < n else ("p", "}")
                if nxt in (("p", ","), ("p", "}")):
                    key = ".".join([s for s in stack if s] + [pending])
                    out.append({"locale": loc, "key": key, "value": toks[j][1]})
                    pending = None
                    j += 1
                    continue
            continue
        j += 1
    return j


def is_i18n_file(rel: str, globs: List[str]) -> bool:
    return any(fnmatch.fnmatch(rel, g) for g in globs)


def extract_tree(root: Path, rules: Dict[str, Any], restrict: Optional[set] = None) -> List[Dict[str, str]]:
    cfg = rules["i18n"]
    rows: List[Dict[str, str]] = []
    for rel in walk_files(root):
        r = str(rel)
        if not r.endswith((".ts", ".tsx", ".js")) or not is_i18n_file(r, cfg["file_globs"]):
            continue
        if restrict is not None and r not in restrict:
            continue
        src = read_text(root / rel) or ""
        for row in extract_file(src, cfg["locale_export_names"], r):
            row["file"] = r
            rows.append(row)
    return rows


def diff_strings(old_rows: List[Dict[str, str]], new_rows: List[Dict[str, str]], locales: List[str]) -> Dict[str, Any]:
    def visible(v: str) -> bool:
        # ссылки вида $t(key) — не текст, а указатель на другой ключ
        return bool(v.strip()) and not re.fullmatch(r"\s*\$t\([^)]*\)\s*", v)

    old_rows = [r for r in old_rows if visible(r["value"])]
    new_rows = [r for r in new_rows if visible(r["value"])]

    def by_value(rows):
        d: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
        for r in rows:
            d[r["locale"]][r["value"]].append(f"{r['file']}::{r['key']}")
        return d

    def by_key(rows):
        return {(r["locale"], r["file"], r["key"]): r["value"] for r in rows}

    ov, nv = by_value(old_rows), by_value(new_rows)
    ok, nk = by_key(old_rows), by_key(new_rows)
    result: Dict[str, Any] = {"locales": {}}
    for loc in locales:
        o, n = ov.get(loc, {}), nv.get(loc, {})
        added = sorted(v for v in n if v not in o and v.strip())
        removed = sorted(v for v in o if v not in n and v.strip())
        result["locales"][loc] = {
            "old_count": len(o),
            "new_count": len(n),
            "added": [{"value": v, "where": n[v][:5]} for v in added],
            "removed": [{"value": v, "where": o[v][:5]} for v in removed],
        }
    changed = []
    for key, old_val in ok.items():
        if key in nk and nk[key] != old_val and key[0] in locales:
            changed.append({"locale": key[0], "file": key[1], "key": key[2], "old": old_val, "new": nk[key]})
    result["changed_same_key"] = changed
    return result


def locale_dirs(root: Path, app_translations: str) -> List[str]:
    d = root / app_translations
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []
