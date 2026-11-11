"""Задачи Jira из ручной выгрузки (правила В1–В2).

Поддерживаемые форматы в каталоге inputs/jira (любой из):
* XML  — Jira «Export → XML» (RSS с <item>);
* CSV  — Jira «Export → CSV»;
* JSON — список объектов {key, summary, description, type, status, labels};
* TXT/MD — ручной текст: строка «GUI-1234 [point] Название», далее описание до следующего ключа.

Метка продукта — префикс в квадратных скобках в начале названия: «[point] …».
"""

from __future__ import annotations

import csv
import html
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List

from .common import log, read_text

KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
PREFIX_RE = re.compile(r"^\s*((?:\[[^\]]+\]\s*)+)")


def product_tags(summary: str) -> List[str]:
    m = PREFIX_RE.match(summary or "")
    if not m:
        return []
    return [t.strip().lower() for t in re.findall(r"\[([^\]]+)\]", m.group(1))]


def _clean_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"</p>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(s)).strip()


def _from_xml(path: Path) -> List[Dict[str, Any]]:
    out = []
    root = ET.parse(path).getroot()
    for item in root.iter("item"):
        def t(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        summary = t("summary") or re.sub(r"^\[[^\]]+\]\s*", "", t("title"))
        out.append({
            "key": t("key"),
            "summary": summary,
            "description": _clean_html(t("description")),
            "type": t("type"),
            "status": t("status"),
            "labels": [l.text for l in item.iter("label") if l.text],
            "components": [c.text for c in item.iter("component") if c.text],
            "fix_versions": [c.text for c in item.iter("fixVersion") if c.text],
        })
    return out


def _from_csv(path: Path) -> List[Dict[str, Any]]:
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        idx: Dict[str, List[int]] = {}
        for i, h in enumerate(header):
            idx.setdefault(h.strip(), []).append(i)

        def col(row, *names):
            vals = []
            for nm in names:
                for i in idx.get(nm, []):
                    if i < len(row) and row[i].strip():
                        vals.append(row[i].strip())
            return vals

        for row in reader:
            key = (col(row, "Issue key", "Ключ задачи") or [""])[0]
            if not key:
                continue
            out.append({
                "key": key,
                "summary": (col(row, "Summary", "Тема") or [""])[0],
                "description": (col(row, "Description", "Описание") or [""])[0],
                "type": (col(row, "Issue Type", "Тип задачи") or [""])[0],
                "status": (col(row, "Status", "Статус") or [""])[0],
                "labels": col(row, "Labels", "Метки"),
                "components": col(row, "Component/s", "Компоненты"),
                "fix_versions": col(row, "Fix Version/s", "Исправить в версиях"),
            })
    return out


def _from_text(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cur = None
    for line in (read_text(path) or "").splitlines():
        m = re.match(r"^\s*(?:[#*\-]+\s*)?([A-Z][A-Z0-9]+-\d+)\s*[:\-–]?\s*(.*)$", line)
        if m:
            cur = {"key": m.group(1), "summary": m.group(2).strip(), "description": "", "type": "", "status": "",
                   "labels": [], "components": [], "fix_versions": []}
            out.append(cur)
        elif cur is not None:
            cur["description"] += line + "\n"
    for c in out:
        c["description"] = c["description"].strip()
    return out


def load_dir(d: Path) -> Dict[str, Dict[str, Any]]:
    return load_files(sorted(d.iterdir()) if d.is_dir() else [])


def load_files(paths: List[Path]) -> Dict[str, Dict[str, Any]]:
    issues: Dict[str, Dict[str, Any]] = {}
    for p in paths:
        try:
            if p.suffix.lower() == ".xml":
                rows = _from_xml(p)
            elif p.suffix.lower() == ".csv":
                rows = _from_csv(p)
            elif p.suffix.lower() == ".json":
                data = json.loads(read_text(p) or "[]")
                rows = data if isinstance(data, list) else data.get("issues", [])
            elif p.suffix.lower() in (".txt", ".md"):
                rows = _from_text(p)
            else:
                continue
        except Exception as e:  # noqa: BLE001
            log(f"Jira: не удалось разобрать {p.name}: {e}")
            continue
        for r in rows:
            if r.get("key"):
                r["product_tags"] = product_tags(r.get("summary", ""))
                r["source_file"] = p.name
                issues[r["key"]] = r
    log(f"Jira: загружено задач — {len(issues)}")
    return issues
