"""Работа с исходниками документации (MkDocs, docs/src/MD_<lang>/docs).

* index_docs   — индекс базовой доки: заголовки, картинки, include, термины в кавычках;
* diff_docs    — дифф двух деревьев доки по языкам (json + html);
* prepare_workspace — копия текстов базовой доки для правок агентом.
"""

from __future__ import annotations

import difflib
import html as H
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import read_text, save_json, save_text, walk_files

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*(\{#([\w\-]+)\})?\s*$")
IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)")
INCLUDE_RE = re.compile(r"""\{%\s*include\s+['"]([^'"]+)['"]""")
QUOTED_RE = re.compile(r"[«“\"]([^«»“”\"\n]{2,80})[»”\"]")
VAR_RE = re.compile(r"\{\{\s*([A-Z_]+)\s*\}\}")
TEXT_SUFFIX = {".md", ".pages", ".yml", ".yaml"}


def docs_root(doc_repo: Path, lang: str) -> Path:
    return doc_repo / "docs" / "src" / f"MD_{lang}" / "docs"


def index_docs(doc_repo: Path, langs: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for lang in langs:
        root = docs_root(doc_repo, lang)
        files = []
        for rel in walk_files(root):
            if rel.suffix != ".md":
                continue
            text = read_text(root / rel) or ""
            heads, imgs, incs = [], [], []
            for ln, line in enumerate(text.splitlines(), 1):
                m = HEADING_RE.match(line)
                if m:
                    heads.append({"line": ln, "level": len(m.group(1)), "text": m.group(2), "anchor": m.group(4)})
                for a, p in IMG_RE.findall(line):
                    imgs.append({"line": ln, "alt": a, "path": p})
                for inc in INCLUDE_RE.findall(line):
                    incs.append({"line": ln, "path": inc})
            files.append({
                "path": str(rel),
                "lines": text.count("\n") + 1,
                "headings": heads,
                "images": imgs,
                "includes": incs,
                "variables": sorted(set(VAR_RE.findall(text))),
                "quoted_terms": sorted(set(QUOTED_RE.findall(text)))[:300],
            })
        out[lang] = {"root": str(root), "files": files}
    return out


# ---------------------------------------------------------------- дифф доки

def _text_files(root: Path) -> Dict[str, Path]:
    return {str(r): root / r for r in walk_files(root) if (r.suffix in TEXT_SUFFIX or r.name == ".pages")}


def _img_files(root: Path) -> Dict[str, Path]:
    return {str(r): root / r for r in walk_files(root) if r.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".svg")}


def diff_docs(old_repo: Path, new_repo: Path, langs: List[str], old_label: str, new_label: str,
              old_root_fn=docs_root, new_root_fn=docs_root) -> Dict[str, Any]:
    result: Dict[str, Any] = {"old": old_label, "new": new_label, "langs": {}}
    for lang in langs:
        a, b = old_root_fn(old_repo, lang), new_root_fn(new_repo, lang)
        ta, tb = _text_files(a), _text_files(b)
        files = []
        for rel in sorted(set(ta) | set(tb)):
            oa = (read_text(ta[rel]) if rel in ta else None)
            ob = (read_text(tb[rel]) if rel in tb else None)
            if oa == ob:
                continue
            status = "modified" if (oa is not None and ob is not None) else ("added" if ob is not None else "deleted")
            patch = "".join(difflib.unified_diff((oa or "").splitlines(True), (ob or "").splitlines(True),
                                                 fromfile=f"{old_label}/{rel}", tofile=f"{new_label}/{rel}", n=2))
            add = sum(1 for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
            rem = sum(1 for l in patch.splitlines() if l.startswith("-") and not l.startswith("---"))
            files.append({"path": rel, "status": status, "added": add, "removed": rem, "patch": patch})
        imgs = []
        ia, ib = _img_files(a), _img_files(b)
        for rel in sorted(set(ia) | set(ib)):
            if rel in ia and rel in ib:
                if ia[rel].read_bytes() != ib[rel].read_bytes():
                    imgs.append({"path": rel, "status": "modified"})
            elif rel in ib:
                imgs.append({"path": rel, "status": "added"})
            else:
                imgs.append({"path": rel, "status": "deleted"})
        result["langs"][lang] = {"files": files, "images": imgs}
    return result


def render_diff_html(diff: Dict[str, Any], title: str) -> str:
    def esc(s):
        return H.escape(s, quote=False)

    parts = [f"""<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1c2230;--muted:#5c667a;--line:#dde1e8;--add:#e6f4ea;--del:#fbeaea;--acc:#2a5bd7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151b;--card:#1a1e26;--fg:#e4e7ee;--muted:#9aa3b5;--line:#2c3340;--add:#16301f;--del:#3a1c1e;--acc:#7ea2ff}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;padding:24px max(16px,4vw)}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:28px 0 8px}} .muted{{color:var(--muted)}}
table{{border-collapse:collapse;background:var(--card);border:1px solid var(--line)}} td,th{{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left}}
details{{background:var(--card);border:1px solid var(--line);border-radius:6px;margin:8px 0}} summary{{cursor:pointer;padding:8px 12px;font-family:ui-monospace,monospace}}
pre{{margin:0;padding:8px 0;overflow-x:auto;font:12px/1.55 ui-monospace,monospace}} .l{{display:block;padding:0 12px;white-space:pre}}
.a{{background:var(--add)}} .d{{background:var(--del)}} .h{{color:var(--muted)}} a{{color:var(--acc)}}
</style><body><h1>{esc(title)}</h1><p class="muted">{esc(diff['old'])} → {esc(diff['new'])}</p>"""]
    parts.append("<table><tr><th>Язык</th><th>Текстовых файлов изменено</th><th>Картинок изменено</th></tr>")
    for lang, d in diff["langs"].items():
        parts.append(f"<tr><td><a href='#{lang}'>{lang}</a></td><td>{len(d['files'])}</td><td>{len(d['images'])}</td></tr>")
    parts.append("</table>")
    for lang, d in diff["langs"].items():
        parts.append(f"<h2 id='{lang}'>{lang}</h2>")
        for f in d["files"]:
            lines = []
            for l in f["patch"].splitlines():
                cls = "a" if l.startswith("+") and not l.startswith("+++") else "d" if l.startswith("-") and not l.startswith("---") else "h" if l.startswith("@@") else ""
                lines.append(f"<span class='l {cls}'>{esc(l) or ' '}</span>")
            parts.append(f"<details><summary>{esc(f['path'])} · {f['status']} · +{f['added']} −{f['removed']}</summary><pre>{''.join(lines)}</pre></details>")
        if d["images"]:
            parts.append("<p class='muted'>Картинки: " + ", ".join(f"{esc(i['path'])} ({i['status']})" for i in d["images"]) + "</p>")
    parts.append("</body></html>")
    return "".join(parts)


def write_diff(diff: Dict[str, Any], out_dir: Path, name: str, title: str) -> None:
    save_json(out_dir / f"{name}.json", diff)
    save_text(out_dir / f"{name}.html", render_diff_html(diff, title))
    for lang, d in diff["langs"].items():
        save_text(out_dir / f"{name}_{lang}.diff", "".join(f["patch"] for f in d["files"]))


# ---------------------------------------------------------------- рабочая папка

def prepare_workspace(base_repo: Path, out_dir: Path, langs: List[str], force: bool = False) -> List[str]:
    """Копирует текстовые файлы базовой доки в out_dir/MD_<lang>/docs. Картинки не копируются."""
    made = []
    for lang in langs:
        src = docs_root(base_repo, lang)
        dst = out_dir / f"MD_{lang}" / "docs"
        if dst.exists() and not force:
            made.append(f"{dst} (уже существует, не трогаю; --force чтобы пересоздать)")
            continue
        if dst.exists():
            shutil.rmtree(dst)
        for rel, p in _text_files(src).items():
            t = dst / rel
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, t)
        made.append(str(dst))
    return made


def workspace_root(ws: Path, lang: str) -> Path:
    return ws / f"MD_{lang}" / "docs"
