"""Граф импортов монорепы: какие файлы достижимы из приложения (правила В3–В6).

Нативный разбор: регулярные выражения по import/export/require и резолвер путей
(относительные, workspace-пакеты @vlabs/* и @vlapps/*, tsconfig paths пакетов).
Это эвристика, а не компилятор: неразрешённые импорты собираются в отчёт.
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .common import load_json, load_jsonc, read_text

CODE_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
RESOLVE_EXT = CODE_EXT + (".json", ".sass", ".scss", ".css", ".svg")

SPEC_RES = [
    re.compile(r"""\bfrom\s*['"]([^'"\n]+)['"]"""),
    re.compile(r"""\bimport\s*['"]([^'"\n]+)['"]"""),
    re.compile(r"""\bimport\(\s*['"]([^'"\n]+)['"]\s*\)"""),
    re.compile(r"""\brequire\(\s*['"]([^'"\n]+)['"]\s*\)"""),
]


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


class Resolver:
    def __init__(self, root: Path):
        self.root = root
        self.packages: Dict[str, Path] = {}   # имя пакета → каталог
        self.main: Dict[str, Optional[str]] = {}
        self.ts_paths: Dict[Path, List[Tuple[str, List[str]]]] = {}
        for group in ("packages", "apps"):
            gdir = root / group
            if not gdir.is_dir():
                continue
            for d in sorted(gdir.iterdir()):
                pj = d / "package.json"
                if pj.is_file():
                    try:
                        data = load_json(pj)
                    except Exception:
                        continue
                    name = data.get("name")
                    if name:
                        self.packages[name] = d
                        self.main[name] = data.get("main")
                    ts = load_jsonc(d / "tsconfig.json") or {}
                    paths = (ts.get("compilerOptions") or {}).get("paths") or {}
                    self.ts_paths[d] = sorted(paths.items(), key=lambda kv: -len(kv[0]))

    def _try_file(self, base: Path) -> Optional[Path]:
        if base.is_file():
            return base
        for ext in RESOLVE_EXT:
            p = base.with_name(base.name + ext)
            if p.is_file():
                return p
        if base.is_dir():
            for ext in CODE_EXT:
                p = base / f"index{ext}"
                if p.is_file():
                    return p
        return None

    def _pkg_of(self, f: Path) -> Optional[Path]:
        for d in self.ts_paths:
            try:
                f.relative_to(d)
                return d
            except ValueError:
                continue
        return None

    def resolve(self, spec: str, importer: Path) -> Optional[Path]:
        spec = spec.split("?")[0]
        if spec.startswith("."):
            return self._try_file((importer.parent / spec).resolve())
        # workspace-пакет: @scope/name[/sub]
        if spec.startswith("@"):
            parts = spec.split("/")
            name = "/".join(parts[:2])
            sub = "/".join(parts[2:])
            d = self.packages.get(name)
            if d is not None:
                if sub:
                    return self._try_file(d / sub) or self._try_file(d / "src" / sub)
                main = self.main.get(name)
                return (self._try_file(d / main) if main else None) or self._try_file(d / "index") or self._try_file(d / "src" / "index")
            return None  # внешняя зависимость
        # tsconfig paths пакета, где лежит импортирующий файл
        pkg = self._pkg_of(importer)
        if pkg is not None:
            for key, targets in self.ts_paths.get(pkg, []):
                if key.endswith("/*") and spec.startswith(key[:-1]):
                    rest = spec[len(key) - 1:]
                    for t in targets:
                        cand = self._try_file((pkg / t.replace("*", rest)).resolve())
                        if cand:
                            return cand
                elif key == spec:
                    for t in targets:
                        cand = self._try_file((pkg / t).resolve())
                        if cand:
                            return cand
        return None


def specs_of(src: str) -> List[str]:
    src = strip_comments(src)
    out: List[str] = []
    for rx in SPEC_RES:
        out.extend(rx.findall(src))
    return out


def reachable_from(root: Path, entry_dirs: List[str]) -> Dict[str, object]:
    """BFS от всех файлов entry_dirs. Возвращает множество достижимых путей (относительно root)."""
    root = root.resolve()
    res = Resolver(root)
    queue: deque = deque()
    seen: Set[Path] = set()
    for ed in entry_dirs:
        d = root / ed
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file() and p.suffix in CODE_EXT and "node_modules" not in p.parts:
                queue.append(p.resolve())
    unresolved: Dict[str, int] = {}
    while queue:
        f = queue.popleft()
        if f in seen:
            continue
        seen.add(f)
        if f.suffix not in CODE_EXT:
            continue
        src = read_text(f) or ""
        for spec in specs_of(src):
            tgt = res.resolve(spec, f)
            if tgt is None:
                if spec.startswith(".") or spec.startswith("@vlabs/") or spec.startswith("@vlapps/"):
                    unresolved[spec] = unresolved.get(spec, 0) + 1
                continue
            if tgt not in seen:
                queue.append(tgt)
    rel = sorted(str(p.relative_to(root)) for p in seen if root in p.parents or p == root)
    return {"files": rel, "unresolved_internal": dict(sorted(unresolved.items(), key=lambda kv: -kv[1])[:200])}
