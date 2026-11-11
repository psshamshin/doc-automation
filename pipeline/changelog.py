"""Разбор CHANGELOG.md приложения и выборка записей в границах релиза (правила А1–А2, Б3)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from .common import read_text, semver_key

VERSION_RE = re.compile(r"^##\s*\[(?P<ver>[^\]]+)\]\s*(?P<date>\S+)?")
SECTION_RE = re.compile(r"^###\s*(?P<name>.+?)\s*$")
TAGS_RE = re.compile(r"^((?:\[[^\]]+\])+)")
TASK_RE = re.compile(r"`?\b((?:GUI|PASS|LP|VL)-\d+)\b`?")

SECTION_TYPES = {
    "Добавлено": "add",
    "Изменено": "change",
    "Исправлено": "fix",
    "Удалено": "remove",
}


def parse_line(raw: str) -> Dict[str, Any]:
    """'* [monokit][entities/x] `GUI-1` - текст' → теги, задача, текст, упомянутые идентификаторы."""
    body = raw.lstrip("*").strip()
    tags: List[str] = []
    m = TAGS_RE.match(body)
    if m:
        tags = re.findall(r"\[([^\]]+)\]", m.group(1))
        body = body[m.end():].strip()
    task = None
    tm = TASK_RE.search(body)
    if tm and tm.start() < 20:  # номер задачи стоит в начале записи
        task = tm.group(1)
        body = (body[: tm.start()] + body[tm.end():]).strip()
    body = re.sub(r"^[-–—:]\s*", "", body).strip()
    code_refs = re.findall(r"`([^`]+)`", body)
    return {"tags": tags, "task": task, "text": body, "code_refs": code_refs, "raw": raw.strip()}


def parse_changelog(path: Path) -> List[Dict[str, Any]]:
    text = read_text(path)
    if text is None:
        raise FileNotFoundError(path)
    versions: List[Dict[str, Any]] = []
    cur_ver = None
    cur_sec = None
    last_entry = None
    for line in text.splitlines():
        vm = VERSION_RE.match(line)
        if vm:
            cur_ver = {"version": vm.group("ver").strip(), "date": vm.group("date"), "entries": []}
            versions.append(cur_ver)
            cur_sec, last_entry = None, None
            continue
        sm = SECTION_RE.match(line)
        if sm and cur_ver is not None:
            cur_sec = sm.group("name")
            last_entry = None
            continue
        if cur_ver is None:
            continue
        if line.lstrip().startswith("* "):
            entry = parse_line(line.strip())
            entry["section"] = cur_sec
            entry["type"] = SECTION_TYPES.get(cur_sec or "", "other")
            entry["version"] = cur_ver["version"]
            cur_ver["entries"].append(entry)
            last_entry = entry
        elif line.strip() and last_entry is not None and not line.startswith("#"):
            # перенос строки внутри записи
            last_entry["text"] += " " + line.strip()
            last_entry["raw"] += "\n" + line.rstrip()
    return versions


def select_range(versions: List[Dict[str, Any]], lower: str, upper: str) -> List[Dict[str, Any]]:
    """Версии строго выше lower и не выше upper (правило А2)."""
    lo, hi = semver_key(lower), semver_key(upper)
    out = []
    for v in versions:
        k = semver_key(v["version"])
        if lo < k <= hi:
            out.append(v)
    return out


def collect(path: Path, lower: str, upper: str) -> Dict[str, Any]:
    versions = parse_changelog(path)
    selected = select_range(versions, lower, upper)
    entries = []
    for v in selected:
        for i, e in enumerate(v["entries"]):
            e = dict(e)
            e["id"] = f"{v['version']}#{i + 1}"
            entries.append(e)
    return {
        "source": str(path),
        "lower": lower,
        "upper": upper,
        "versions": [{"version": v["version"], "date": v["date"], "count": len(v["entries"])} for v in selected],
        "entries": entries,
        "outside_range_versions": [v["version"] for v in versions if v not in selected][:10],
    }
