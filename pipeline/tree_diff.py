"""Дифф двух деревьев исходников (lerna на двух тегах) без git.

Для каждого изменённого файла: статус, категория (правила Г1–Г4), размер изменения
и unified-патч в отдельном файле.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import is_text_file, read_text, save_text, walk_files


def _digest(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def categorize(rel: str, rules: Dict[str, Any]) -> str:
    """Первая подходящая категория из rules['file_categories'] (порядок важен)."""
    for cat in rules["file_categories"]:
        for pat in cat["globs"]:
            if fnmatch.fnmatch(rel, pat):
                return cat["name"]
    return "source"


def scope_of(rel: str) -> Dict[str, Optional[str]]:
    """apps/<app>/… или packages/<pkg>/… → область кода."""
    parts = rel.split("/")
    if len(parts) >= 2 and parts[0] in ("apps", "packages"):
        return {"area": parts[0], "name": parts[1], "inner": "/".join(parts[2:])}
    return {"area": "root", "name": None, "inner": rel}


def unified(old: Optional[str], new: Optional[str], rel: str, context: int = 3) -> str:
    a = (old or "").splitlines(keepends=True)
    b = (new or "").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}", n=context))


def count_changes(patch: str):
    add = sum(1 for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
    rem = sum(1 for l in patch.splitlines() if l.startswith("-") and not l.startswith("---"))
    return add, rem


def diff_trees(old_root: Path, new_root: Path, rules: Dict[str, Any], patches_dir: Optional[Path]) -> Dict[str, Any]:
    exclude = rules.get("diff_exclude_globs", [])

    def excluded(rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, g) for g in exclude)

    old_files = {str(p): p for p in walk_files(old_root) if not excluded(str(p))}
    new_files = {str(p): p for p in walk_files(new_root) if not excluded(str(p))}
    all_rel = sorted(set(old_files) | set(new_files))

    changed: List[Dict[str, Any]] = []
    for rel in all_rel:
        o = old_root / rel if rel in old_files else None
        n = new_root / rel if rel in new_files else None
        if o is not None and n is not None:
            if o.stat().st_size == n.stat().st_size and _digest(o) == _digest(n):
                continue
            status = "modified"
        elif n is not None:
            status = "added"
        else:
            status = "deleted"

        text = is_text_file(n or o)
        rec: Dict[str, Any] = {
            "path": rel,
            "status": status,
            "category": categorize(rel, rules),
            "binary": not text,
            **scope_of(rel),
        }
        if text:
            patch = unified(read_text(o) if o else None, read_text(n) if n else None, rel)
            rec["added_lines"], rec["removed_lines"] = count_changes(patch)
            if patches_dir is not None and patch:
                pf = patches_dir / (rel.replace("/", "__") + ".diff")
                save_text(pf, patch)
                rec["patch"] = str(pf.relative_to(patches_dir.parent))
        changed.append(rec)

    summary: Dict[str, int] = {}
    for r in changed:
        summary[r["category"]] = summary.get(r["category"], 0) + 1
    return {
        "old_root": str(old_root),
        "new_root": str(new_root),
        "files_compared": len(all_rel),
        "changed_count": len(changed),
        "by_category": summary,
        "files": changed,
    }
