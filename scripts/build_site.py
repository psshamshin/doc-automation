#!/usr/bin/env python3
"""Собирает документацию в сайт MkDocs для просмотра в браузере (три варианта рядом: было, пайплайн, техпис).

Нужны: mkdocs, mkdocs-material, mkdocs-awesome-pages-plugin (pip install ...) и pandoc (таблицы с рамкой).

Пример:
    python3 scripts/build_site.py --version 2.3.0 --out site \
        --variant "было 2.2.0=input/luna-point-2.2.0/docs/src" \
        --variant "пайплайн=output/2.3.0/result@input/luna-point-2.2.0/docs/src" \
        --variant "техпис 2.3.0=../reference/luna-point-2.3.0/docs/src"
    python3 -m http.server -d site 8000     # затем открыть http://localhost:8000

Вариант: `название=папка` или `название=папка@папка_с_картинками`. В папке лежат MD_rus/docs и MD_eng/docs. У рабочей
копии пайплайна картинок нет, их берут из документации прошлого релиза (вторая папка). Переменные документации
({{PRODUCT_NAME}}) читаются из .gitlab-ci.yml репозитория, номер версии (BUILD_MARK) задается --version.
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

LANGS = {"rus": "ru", "eng": "en"}
INCLUDE_RE = re.compile(r"""\{%\s*include\s+['"]([^'"]+)['"]\s*%\}""")
VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
YAML = """site_name: "{title}"
docs_dir: docs
exclude_docs: |
  includes/
theme:
  name: material
  language: {code}
  features: [navigation.sections, navigation.top, search.highlight]
plugins:
  - search
  - awesome-pages
markdown_extensions:
  - tables
  - admonition
  - footnotes
  - attr_list
  - md_in_html
  - toc:
      permalink: true
  - pymdownx.superfences
"""


def ci_variables(root: Path) -> dict:
    for d in [root, *list(root.resolve().parents)[:4]]:
        f = d / ".gitlab-ci.yml"
        if f.is_file():
            out, inside = {}, False
            for line in f.read_text(encoding="utf-8").splitlines():
                if re.match(r"^variables:\s*$", line):
                    inside = True
                elif inside and line and not line.startswith((" ", "\t")):
                    break
                elif inside:
                    m = re.match(r"^\s+([A-Za-z_]\w*):\s*(.*?)\s*$", line)
                    if m:
                        out[m.group(1)] = m.group(2).strip("\"'")
            if out:
                return out
    return {}


def grid_to_html(block: list) -> str:
    """Таблица с рамкой (pandoc) в HTML: python-markdown такие таблицы не понимает."""
    res = subprocess.run(["pandoc", "-f", "markdown", "-t", "html"], input="\n".join(block), text=True, capture_output=True)
    return "\n" + res.stdout + "\n" if res.returncode == 0 and "<table" in res.stdout else "\n".join(block)


def convert_tables(text: str) -> str:
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        if lines[i].startswith("+-") or lines[i].startswith("+="):
            j = i
            while j < len(lines) and lines[j].startswith(("+", "|")):
                j += 1
            out.append(grid_to_html(lines[i:j]))
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def expand(text: str, base: Path, depth: int = 0) -> str:
    """Подключаемые файлы {% include "..." %} вставляются в текст страницы."""
    def one(m):
        f = (base / m.group(1)).resolve()
        return expand(f.read_text(encoding="utf-8"), f.parent, depth + 1) if f.is_file() and depth < 5 else ""
    return INCLUDE_RE.sub(one, text)


def prepare(page: Path, values: dict) -> None:
    text = page.read_text(encoding="utf-8")
    text = expand(text, page.parent)
    text = VAR_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)
    text = re.sub(r"[ \t]*\{-\}[ \t]*$", "", text, flags=re.M)         # {-}: разметка pandoc "без номера"
    text = text.replace("\\clearpage", "")
    page.write_text(convert_tables(text), encoding="utf-8")


def build(label: str, src: Path, images: Path, values: dict, out: Path, stage: Path) -> list:
    done = []
    for lang, code in LANGS.items():
        docs = stage / label / lang / "docs"
        if docs.exists():
            shutil.rmtree(docs)
        for base in (images, src):                                     # сначала картинки и все файлы, затем страницы поверх
            root = base / f"MD_{lang}" / "docs"
            if root.is_dir():
                shutil.copytree(root, docs, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".DS_Store"))
        if not docs.is_dir():
            continue
        for page in sorted(docs.rglob("*.md")):
            prepare(page, values)
        (docs.parent / "mkdocs.yml").write_text(YAML.format(title=f"{values.get('PRODUCT_NAME', 'Документация')}: {label}", code=code), encoding="utf-8")
        slug = re.sub(r"\W+", "_", label)
        target = out / slug / lang
        res = subprocess.run([sys.executable, "-m", "mkdocs", "build", "-q", "-f", str(docs.parent / "mkdocs.yml"), "-d", str(target)],
                             capture_output=True, text=True)
        print(f"{label} [{lang}]: " + ("готово" if res.returncode == 0 else "ошибка\n" + res.stderr[-600:]))
        if res.returncode == 0:
            done.append((lang, f"{slug}/{lang}/index.html"))
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", action="append", required=True, help="название=папка[@папка_с_картинками]")
    ap.add_argument("--version", required=True, help="номер версии для переменной BUILD_MARK")
    ap.add_argument("--out", default="site")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    stage = out / "_stage"
    rows = []
    for v in a.variant:
        label, _, paths = v.partition("=")
        src, _, img = paths.partition("@")
        src = Path(src).resolve()
        images = Path(img).resolve() if img else src
        values = {**ci_variables(images), **ci_variables(src), "BUILD_MARK": a.version}      # у рабочей копии файла CI нет
        rows.append((label, build(label, src, images, values, out, stage)))
    cards = "".join(f"<div class=c><h2>{l}</h2>" + "".join(f"<a href='{u}'>{k}</a>" for k, u in links) + "</div>" for l, links in rows)
    (out / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Документация</title><style>body{font:16px system-ui;margin:40px;max-width:760px}"
        ".c{border:1px solid #ccc;border-radius:8px;padding:12px 20px;margin:12px 0}a{margin-right:16px}</style>"
        f"<h1>Документация {a.version}: варианты</h1>{cards}", encoding="utf-8")
    shutil.rmtree(stage, ignore_errors=True)
    print(f"открыть: python3 -m http.server -d {out} 8000  ->  http://localhost:8000")


if __name__ == "__main__":
    main()
