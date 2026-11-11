"""Версии: чтение из кода и документации, база знаний knowledge/releases.json.

Версия интерфейса берется из package.json приложения. Версия документации, дата и версии
компонентов берутся из последнего файла release notes: заголовок '# [2.2.0] 14.09.2026' и таблица.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import product as P
from .common import load_json, read_text, save_json, semver_key

RN_HEADER_RE = re.compile(r"^#\s*\[(?P<ver>\d+(?:\.\d+){1,3})\]\s*(?P<date>\d{1,2}\.\d{1,2}\.\d{4})?", re.M)
TABLE_ROW_RE = re.compile(r"^\|\s*([A-Za-z][\w\-]*)\s*\|\s*([\w.\-]+)\s*\|\s*$", re.M)


def ui_version_of(code_root: Path) -> Optional[str]:
    """Версия интерфейса: package.json приложения, иначе верхняя версия ченджлога, иначе имя папки."""
    pj = code_root / P.APP_PACKAGE_JSON
    if pj.is_file():
        try:
            v = load_json(pj).get("version")
            if v:
                return str(v)
        except Exception:  # noqa: BLE001
            pass
    text = read_text(code_root / P.APP_CHANGELOG) or ""
    m = re.search(r"^##\s*\[(\d+\.\d+\.\d+)\]", text, flags=re.M)
    if m:
        return m.group(1)
    m = re.search(r"(\d+\.\d+\.\d+)", code_root.name)
    return m.group(1) if m else None


def parse_release_notes(text: str) -> Optional[Dict[str, Any]]:
    m = RN_HEADER_RE.search(text or "")
    if not m:
        return None
    return {"version": m.group("ver"), "date": m.group("date"),
            "components": {name: ver for name, ver in TABLE_ROW_RE.findall(text)}}


def docs_releases(docs_repo: Path, lang: str = "rus") -> List[Dict[str, Any]]:
    """Все релизы, описанные в release notes документации, от нового к старому."""
    d = docs_repo / "docs" / "src" / f"MD_{lang}" / "docs" / P.RELEASE_NOTES_DIR
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.md")):
            info = parse_release_notes(read_text(p) or "")
            if info:
                info["file"] = p.name
                out.append(info)
    return sorted(out, key=lambda r: semver_key(r["version"]), reverse=True)


def release_notes_filename(version: str) -> str:
    """2.3.0 -> 230.md, 1.14.1 -> 1141.md (так названы файлы в документации)."""
    return "".join(re.findall(r"\d+", version)) + ".md"


def next_minor(version: str) -> str:
    a, b, _ = semver_key(version)
    return f"{a}.{b + 1}.0"


# ---------------------------------------------------------------- база знаний

def load_knowledge(path: Path) -> Dict[str, Any]:
    if path.is_file():
        return load_json(path)
    return {"_about": "", "releases": []}


def find_by_ui(knowledge: Dict[str, Any], ui_version: str) -> Optional[Dict[str, Any]]:
    for r in knowledge.get("releases", []):
        if r.get("ui") == ui_version:
            return r
    return None


def find_release(knowledge: Dict[str, Any], version: str) -> Optional[Dict[str, Any]]:
    for r in knowledge.get("releases", []):
        if r.get("release") == version:
            return r
    return None


def remember(path: Path, entries: List[Dict[str, Any]]) -> List[str]:
    """Добавляет в базу знаний релизы, которых там нет, и дополняет пустые поля. Возвращает, что изменилось."""
    kn = load_knowledge(path)
    changed = []
    for e in entries:
        cur = find_release(kn, e["release"])
        if cur is None:
            kn.setdefault("releases", []).append({k: v for k, v in e.items() if v not in (None, {}, [])})
            changed.append(f"добавлен релиз {e['release']}")
            continue
        for k, v in e.items():
            if v not in (None, {}, []) and cur.get(k) in (None, {}, [], ""):
                cur[k] = v
                changed.append(f"релиз {e['release']}: заполнено поле {k}")
    if changed:
        kn["releases"] = sorted(kn["releases"], key=lambda r: semver_key(r["release"]), reverse=True)
        save_json(path, kn)
    return changed
