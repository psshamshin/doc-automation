"""Общие утилиты: конфиг, пути, чтение и запись файлов, логирование."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

ROOT = Path(__file__).resolve().parent.parent  # каталог doc-automation

SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".nx", ".cache", "coverage", "__pycache__"}

TEXT_EXT = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".md", ".sass", ".scss", ".css",
    ".html", ".yml", ".yaml", ".txt", ".svg", ".toml", ".csv", ".xml", ".pages", ".py", ".sh",
}


def log(msg: str) -> None:
    print(f"[doc-automation] {msg}", file=sys.stderr, flush=True)


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return None
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXT or path.name in {".pages", "Dockerfile"}:
        return True
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
        return b"\0" not in chunk
    except OSError:
        return False


def walk_files(root: Path, skip_dirs: Iterable[str] = SKIP_DIRS) -> Iterator[Path]:
    """Все файлы под root (относительные пути), без служебных каталогов."""
    skip = set(skip_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        for name in sorted(filenames):
            if name == ".DS_Store":
                continue
            yield Path(dirpath, name).relative_to(root)


def strip_json_comments(text: str) -> str:
    """tsconfig.json допускает комментарии и висячие запятые."""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    cleaned = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", cleaned)


def load_jsonc(path: Path) -> Any:
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(strip_json_comments(text))
    except json.JSONDecodeError:
        return None


class Config:
    """Конфиг релиза + правила + карта доки. Пути считаются от каталога doc-automation."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.data: Dict[str, Any] = load_json(self.path)
        base = self.path.parent.parent  # config/<file> → doc-automation
        self.base = base
        self.rules: Dict[str, Any] = load_json(self._p(self.data.get("rules", "config/rules.json")))
        self.doc_map: Dict[str, Any] = load_json(self._p(self.data.get("doc_map", "config/doc_map.json")))

    def _p(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.base / p)

    def path_of(self, key: str) -> Path:
        return self._p(self.data["paths"][key])

    @property
    def release(self) -> str:
        return self.data["release"]

    @property
    def run_dir(self) -> Path:
        return self._p(self.data.get("run_dir", f"runs/{self.release}"))

    @property
    def langs(self) -> List[str]:
        return self.data.get("doc_langs", ["rus", "eng"])

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


def semver_key(v: str):
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts[:3]) + (0,) * (3 - min(3, len(parts)))
